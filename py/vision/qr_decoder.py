"""
二维码/条形码解码器
==================
本文件的职责：
- 提供 QRDecoder 类，用于从图像帧中解码二维码和条形码
- 支持 QR Code、Code128、EAN 等主流码制
- 提供 match_qr_to_detections 函数，将二维码与 YOLO 检测框进行空间匹配
- 依赖库：pyzbar（必须安装）、numpy
- 使用场景：摄像头实时识别时，先用 YOLO 检测物体，再用本模块读取物体上的二维码

典型调用流程：
1. 创建 QRDecoder 实例
2. 在摄像头循环中调用 decoder.decode(frame) 获取所有二维码
3. 调用 match_qr_to_detections() 将二维码绑定到 YOLO 检测结果
4. 根据绑定关系显示或处理二维码内容

版本历史（__version__）：
- v1.0（2026-09-29）：初版——4 级直读（CLAHE/2×/Otsu/锐化二值）+ minAreaRect
  估角 + last_ok 角复用 + 90° 兜底 + ROI 外扩裁剪；19 张真实帧基线 4/19。
  同期诞生实时取证工具 scan_test.py（存的帧就是今天的 fixtures）。
- v1.1（2026-10-01）：新增第 3 级「轮廓矫正兜底」——Otsu 双极性×5 闭核找
  码面轮廓 → 极角排序四角 → 4 档静区 margin×6 档画布尺寸透视矫正 →
  CLAHE(1/3/5)+Otsu+2/4× 放大 → ZBar；41 个视图（全帧+2/3×LANCZOS+36 裁区）；
  跨配置投票（同一内容 ≥2 配置命中才返回，压伪码）；rectify_budget_ms=5000
  时间预算。攻帧上限 14/19，回归（120s/帧预算）当前 9/14。
  新增离线回归 tests/test_scan_fixtures.py（19 帧，断言 14 帧真值）。
  已知遗留：兜底搜索耗时（无码帧烧满预算卡预览）+ 5 帧搜索顺序未排到，
  属 v1.2 待办（跨帧续搜 / 解码线程分离，未实现）。
- v1.2（2026-10-08）：v1.2 三机制并入本文件（构造函数 slice_ms/fp_thresh；
  生产默认 _DEFAULT_SLICE_MS=600 已启用，回退只需改回 0）：
  ①时间片——单次兜底最多占 slice_ms（默认试验值 600ms），超时存档返回，
  预览冻结从最坏 6.1s 降到实测 1.08s；②搜索游标——(视图, 轮廓, 静区,
  尺寸, 阶段) 四维断点，把 36 个原子阶段(单次 zbar)跨帧跨调用拼完，零配置
  丢失；③帧指纹门——32×32 灰度指纹三态判"续搜/跳过/重置"，静态且已搜完
  的画面直接跳过，CPU 归零。离线回归：slice=600 命中 9/14 与 v1.1 逐帧
  一致，单次调用最坏 1082ms（对照 v1.1 的 6100ms，降 5.6×）。
  历史实验实现见 tests/slice_decoder.py（现为薄封装，保证实验=正式同一份代码）。
"""
__version__ = "1.2"  # 当前版本，测试脚本启动时打印；历史见上方「版本历史」
from typing import List, Dict, Any  # 类型提示，帮助 IDE 理解数据结构
import time  # 轮廓矫正兜底的时间预算

import cv2  # OpenCV：灰度转换、CLAHE 对比度增强
import numpy as np  # 用于图像数组操作

# 尝试导入 pyzbar 库，如果未安装则设置标志位
# pyzbar 是 Python 的条形码/二维码识别库，底层依赖 libzbar
try:
    from pyzbar import pyzbar
    _HAS_PYZBAR = True  # 标记：pyzbar 可用
except ImportError:
    _HAS_PYZBAR = False  # 标记：pyzbar 不可用，解码功能将禁用


# 直读与估角都失败后的兜底旋转角（度）。
# 1D 条形码（Code128 快递码等）无定位角，ZBar 靠水平扫描线，
# 歪 15°–75° 会直接失败。正常链路是 minAreaRect 估主轴角转 1~2 次。
# 估角失败说明帧质量本身有问题（模糊/低对比），继续轮询期望收益低——
# 视频每秒给约 30 次机会，坏帧直接放弃等下一帧，不为一帧花 300ms。
# 只保留 90°：覆盖长短边猜反的垂直面；-90 与 +90 等价（转 180° 与
# 0° 可扫性也等价），故单点即可。
# 不设时间预算：一次 decode 把候选转完；CPU 压力由上层「扫码间隔」控制。
_FALLBACK_ANGLES = (90.0,)

# minAreaRect 估角后还会附加的垂直候选偏移（双垂直面单 / 长短边猜反）
_PERP_OFFSET = 90.0

# 1D 码制：正常应明显细长；近方形的多为二值化假阳性（如旋转面上的假 I25）
_1D_TYPES = frozenset({
    "CODE128", "CODE39", "CODE93", "I25", "CODABAR",
    "EAN13", "EAN8", "UPCA", "UPCE", "ISBN10", "ISBN13",
    "DATABAR", "DATABAR_EXPANDED",
})
# 2D：允许接近方形
_2D_TYPES = frozenset({"QRCODE", "DATA MATRIX", "DATAMATRIX", "PDF417", "AZTEC"})

# ---------------------------------------------------------------------------
# 扫码模式（全局三档）：qr / barcode / all
# ---------------------------------------------------------------------------
# 三档共用同一条渐进链（直读→估角→旋转→warp 游标，1D 的静区矫正和 QR 的
# 定位角搜索一体化），差异只在 ZBar 的「符号注册表」——pyzbar.decode 的
# symbols 参数决定 ZBar 本帧认哪些码制：
#   qr      只注册 QRCODE：条形码 100% 拼不出，ZBar 的 1D 逐线扫描也整个省掉
#   barcode 只注册 1D 符号：QR 100% 拼不出
#   all     不限符号（完整能力，成本也最高）
# 全局单份：decode 时现场读 _SCAN_MODE，所有 Worker/深度 Worker 即时生效，
# 不用重启不用逐一 set。
_SCAN_MODES = ("qr", "barcode", "all")

# pyzbar 枚举名兼容取值（不同版本成员名略有差异，命中即用）
def _zbar_symbol(*names):
    if not _HAS_PYZBAR:
        return None
    for n in names:
        s = getattr(pyzbar.ZBarSymbol, n, None)
        if s is not None:
            return s
    return None


def _build_symbol_table():
    """按档位预生成符号表：{"qr": (...), "barcode": (...), "all": None}"""
    if not _HAS_PYZBAR:
        return {m: None for m in _SCAN_MODES}
    qr = _zbar_symbol("QRCODE")
    barcode = tuple(filter(None, (
        _zbar_symbol("CODE128"), _zbar_symbol("CODE39"),
        _zbar_symbol("CODE93"),  _zbar_symbol("I25"),
        _zbar_symbol("CODABAR"), _zbar_symbol("EAN13"), _zbar_symbol("EAN8"),
        _zbar_symbol("UPCA", "UPC_A"), _zbar_symbol("UPCE", "UPC_E"),
        _zbar_symbol("ISBN10"), _zbar_symbol("ISBN13"),
        _zbar_symbol("DATABAR"), _zbar_symbol("DATABAR_EXPANDED"),
    )))
    return {"qr": (qr,) if qr else None,
            "barcode": barcode if barcode else None,
            "all": None}


_ZBAR_TABLE = _build_symbol_table()
_SCAN_MODE = "qr"  # 模块级全局（默认二维码；与 config 持久化同源）


def set_scan_mode(mode: str) -> None:
    """切换全局扫码模式（qr/barcode/all）。UI 下拉与本函数一一对应。"""
    global _SCAN_MODE
    if mode in _SCAN_MODES:
        _SCAN_MODE = mode


def get_scan_mode() -> str:
    return _SCAN_MODE


def _zbar_decode(img):
    """pyzbar.decode 按当前模式限定符号注册表后再调用。"""
    if not _HAS_PYZBAR:
        return []
    return pyzbar.decode(img, symbols=_ZBAR_TABLE.get(_SCAN_MODE))


# ---------------------------------------------------------------------------
# 轮廓矫正兜底（rectify fallback）
# ---------------------------------------------------------------------------
# 直读/估角/旋转都失败后的最后一级：找码面轮廓 → minAreaRect 极角排序四角
# → 带静区的透视矫正 → CLAHE+Otsu+最近邻放大 → ZBar 正反扫。
# 实测（19 张真实帧）：主链 4/19，加上本级到 14/19；静区留白是关键
# （贴边映射会切掉 EAN-13 静区，ZBar 能看到码但读错成伪码）。
_RECTIFY_KERNELS = ((9, 31), (9, 21), (11, 41), (7, 19), (5, 15))
# margin=0 贴边映射是 003453/003517/003808 的制胜配置；带静区档防伪码+救其余帧
_RECTIFY_MARGINS = (0.0, 0.06, 0.12, 0.20)   # 静区宽度占比（相对矫正画布）
_RECTIFY_SIZES = ((380, 253), (570, 380), (760, 506), (285, 190),
                  (190, 127), (1140, 759))
_RECTIFY_CLAHE = (1.0, 3.0, 5.0)
_RECTIFY_TOP_CONTOURS = 6

# ---------------------------------------------------------------------------
# v1.2 跨帧渐进式搜索（默认关闭；slice_ms>0 时启用）
# ---------------------------------------------------------------------------
# 问题：解不出的帧在 _decode_rectify 里烧满 rectify_budget_ms（5s）冻结预览，
# 且进度全部作废、下一帧从第 1 个视图重新搜——制胜配置排在深处就永远够不着。
# 方案：把"一次性搜索"改成"可暂停、可续传的有状态搜索"。
#   时间片 slice_ms     单次兜底最多占 slice_ms，超时存档返回（预览不冻结）
#   搜索游标 _cursor/_inner  41 视图×轮廓×留白×尺寸×36 原子阶段 全部可续跑
#   帧指纹门 _update_gate     画面没变→续搜；变了→清零重搜；搜完→跳过
# slice_ms=0 时 decode() 完全走原路径，行为与 v1.1 逐位一致（回归已验证 9/14）。
_DEFAULT_SLICE_MS = 600      # 生产开关：0=关闭（v1.1 原行为）；600=启用渐进式搜索
_DEFAULT_FP_THRESH = 4.0   # 指纹门阈值（32×32 灰度平均绝对差）

# 单个矫正项目的原子阶段数（阶段=一次 pyzbar 调用，断点精确到阶段，零配置丢失）：
#   warp 管线 = 3 CLAHE 档 × (8 次放大正反扫 + 2 次原图正反扫) + 2 次原图兜底 = 32
#   二值路径 = 放大 2/4 × 正反 = 4
_WARP_STAGES = 32
_BIN_STAGES = 4


class QRDecoder:
    """
    二维码/条形码解码器类

    使用方法：
        decoder = QRDecoder()  # 创建实例
        if decoder.available:  # 检查 pyzbar 是否可用
            results = decoder.decode(image)  # 解码图像
    """

    def __init__(self, slice_ms: int = None, fp_thresh: float = None):
        """
        构造函数

        参数：
            slice_ms: v1.2 时间片（毫秒）。0=关闭，完全走 v1.1 原逻辑
                      （默认 _DEFAULT_SLICE_MS）；>0=启用跨帧渐进式搜索
            fp_thresh: 指纹门阈值，仅 slice_ms>0 时生效

        功能：
        - 检查 pyzbar 库是否已安装
        - 初始化解码器状态
        """
        self._available = _HAS_PYZBAR  # 保存 pyzbar 可用状态
        # CLAHE：自适应直方图均衡，提升低光照/反光下的对比度
        # clipLimit=2.0：对比度限制因子，防止过度增强产生噪声
        # tileGridSize=(8,8)：将图像分成8x8小块分别均衡
        self._clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)) if _HAS_PYZBAR else None
        # 上次旋转重试成功的角度：歪码稳态下下一帧先试这个，避免每帧扫全角度
        self._last_ok_angle = 0
        # 轮廓矫正兜底的时间预算（毫秒）。0=不限时（离线回归用）；
        # 生产默认 5000ms——兜底只在主链全灭时才跑，坏帧超时直接放弃等下一帧。
        # 解码跑在相机 Worker 线程（非 GUI 线程），5s 不冻结界面但会拖慢
        # 检测节奏；追求实时可调小，追求成功率可调大。
        self.rectify_budget_ms = 5000
        # —— v1.2 跨帧渐进式搜索（slice_ms=0 时以下状态完全不参与）——
        self.slice_ms = int(slice_ms if slice_ms is not None
                            else _DEFAULT_SLICE_MS)
        self.fp_thresh = float(fp_thresh if fp_thresh is not None
                               else _DEFAULT_FP_THRESH)
        self._fp_ref = None          # 上一帧 32×32 灰度指纹
        self._cursor = None          # 视图级游标（0..40）；None=未开始
        self._inner = None           # 视图内游标 (qi, mi, si, st)
        self._tallies_px = {}        # 跨调用投票箱 content->[count,[results]]
        self._search_done = False    # 搜索空间走完且无结果（同一画面直接跳过）
        self._quick_tried = False    # 本场景快速主链已跑过（静态跳过重复主链）

    def set_slice(self, slice_ms: int, fp_thresh: float = None):
        """
        运行时开关 v1.2 渐进式搜索。

        slice_ms=0：退回 v1.1 原行为（单次烧满 rectify_budget_ms）。
        slice_ms>0：启用时间片 + 搜索游标 + 帧指纹门。
        """
        self.slice_ms = int(slice_ms)
        if fp_thresh is not None:
            self.fp_thresh = float(fp_thresh)
        self._reset_search()

    def progressive_state(self) -> dict:
        """渐进式搜索状态快照（诊断/调参用）。"""
        return {
            "slice_ms": self.slice_ms,
            "cursor": self._cursor,
            "inner": self._inner,
            "done": self._search_done,
            "tallies": {k[:20]: v[0] for k, v in self._tallies_px.items()},
        }

    @property
    def available(self) -> bool:
        """
        只读属性：判断解码器是否可用

        返回：bool 类型
            - True：pyzbar 已安装，可以正常解码
            - False：pyzbar 未安装，解码功能禁用

        使用示例：
            if decoder.available:
                results = decoder.decode(frame)
            else:
                print("请先安装 pyzbar: pip install pyzbar")
        """
        return self._available

    def decode(self, frame: np.ndarray, rois: list = None) -> List[Dict[str, Any]]:
        """
        解码一帧图像中的所有二维码/条形码

        参数：
            frame: OpenCV BGR 或灰度图
            rois: 可选，检测框列表 [[x1,y1,x2,y2], ...]。
                  非空时先对每个框外扩裁剪并放大解码（提升小码成功率），
                  失败再回退整帧解码（整帧同样会 minAreaRect 估角 + 90° 兜底）。

        返回：[{ "bbox": [x1,y1,x2,y2], "content": str, "type": str }, ...]
        """
        if not self._available or frame is None:
            return []

        try:
            # 1) ROI 优先：框外扩约 1/3（给旋转留边），小图再 3x 放大
            if rois:
                results = self._decode_rois(frame, rois)
                if results:
                    if self.slice_ms:
                        self._reset_search()
                    return results

            if len(frame.shape) == 3:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            else:
                gray = frame

            # v1.2 跨帧渐进式搜索（slice_ms>0）：指纹门 + 跳重复主链 + 时间片兜底
            if self.slice_ms:
                return self._decode_progressive(gray)

            codes, ang = self._try_levels_with_rotation(gray)
            if codes:
                return self._map_results_in_crop(codes, ang, gray.shape[:2],
                                                 ox=0, oy=0, scale=1.0)

            # 3) 轮廓矫正兜底：主链（直读/估角/90°）全灭后，找码面轮廓做
            #    带静区的透视矫正再解——歪斜+透视+静区缺失的帧靠这级救回。
            return self._decode_rectify(gray)

        except Exception as e:
            print(f"[QRDecoder] 解码异常: {e}")
            return []

    def _decode_rois(self, frame: np.ndarray, rois: list) -> List[Dict[str, Any]]:
        """对每个 ROI 裁剪放大后解码（含多角度重试），坐标映射回原图。"""
        h, w = frame.shape[:2]
        found = []
        for bbox in rois:
            if not bbox or len(bbox) != 4:
                continue
            x1, y1, x2, y2 = [int(v) for v in bbox]
            # 外扩约 1/3：码可能略超检测框，且歪码旋转后需要边距
            pad_x = max(16, (x2 - x1) // 3)
            pad_y = max(16, (y2 - y1) // 3)
            x1, y1 = max(0, x1 - pad_x), max(0, y1 - pad_y)
            x2, y2 = min(w, x2 + pad_x), min(h, y2 + pad_y)
            if x2 <= x1 or y2 <= y1:
                continue

            crop = frame[y1:y2, x1:x2]
            if len(crop.shape) == 3:
                crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)

            # 小 ROI 放大 3x，补偿 640x480 下码像素不足
            scale = 1.0
            if max(crop.shape[:2]) < 320:
                scale = 3.0
                crop = cv2.resize(crop, None, fx=scale, fy=scale,
                                  interpolation=cv2.INTER_CUBIC)

            codes, ang = self._try_levels_with_rotation(crop)
            if not codes:
                continue
            found.extend(self._map_results_in_crop(
                codes, ang, crop.shape[:2], ox=x1, oy=y1, scale=scale))
        return found

    def _try_levels_with_rotation(self, gray: np.ndarray):
        """
        直读 → minAreaRect 估角转 1~2 次 → 上次成功角 → 90° 单点兜底。

        坏帧（模糊/低对比）估角通常失败或返回无效角，此时不再多角度
        死磕——放弃本帧，等下一帧重来（同物体在视野内有几十帧机会）。

        返回 (codes, angle_deg)：codes 在「旋转后且可能已放大」的图上，
        angle 是相对 gray 做过的旋转（0 表示未转）。
        """
        codes = self._filter_codes(self._try_levels(gray))
        if codes:
            self._last_ok_angle = 0
            return codes, 0

        h, w = gray.shape[:2]
        center = (w // 2, h // 2)

        # 只算一次估角（原实现循环内外各算一次，浪费）
        est = self._estimate_rotate_angles(gray)
        tried = set(est)

        # 1) minAreaRect：估主轴角，通常 1 次转到水平即可扫到
        for ang in est:
            codes = self._try_rotated(gray, center, ang)
            if codes:
                self._last_ok_angle = ang
                return codes, ang

        # 2) 上次成功的角度（歪码稳态，省掉估角误差时的重试）
        if self._last_ok_angle:
            tried.add(self._last_ok_angle)
            codes = self._try_rotated(gray, center, self._last_ok_angle)
            if codes:
                return codes, self._last_ok_angle

        # 3) 90° 单点兜底：估角/last_ok 都失败，说明帧本身有问题，
        #    只补一次垂直向就放弃（估角已含 90° 时去重跳过）
        for ang in _FALLBACK_ANGLES:
            if ang in tried:
                continue
            codes = self._try_rotated(gray, center, ang)
            if codes:
                self._last_ok_angle = ang
                return codes, ang
        return [], 0

    def _try_rotated(self, gray: np.ndarray, center, ang: float):
        """从原 gray 转 ang 度后 4 级解码（避免连续旋转累积模糊）。"""
        M = cv2.getRotationMatrix2D(center, float(ang), 1.0)
        rotated = cv2.warpAffine(
            gray, M, (gray.shape[1], gray.shape[0]),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=255,  # 码多为白底
        )
        return self._filter_codes(self._try_levels(rotated))

    def _estimate_rotate_angles(self, gray: np.ndarray) -> List[float]:
        """
        minAreaRect 估计码主轴，返回应旋转到接近水平的角列表（度）。

        OpenCV 4.x minAreaRect 角度约定（实测）：
        - 长边为 h（h>w）时 ang∈(0,90]，phi = ang - 90（多为负，对应画布逆时针转过）
        - 长边为 w（w≥h）时 ang∈[0,90]，phi = ang（多为正，对应画布顺时针转过）
        再附加 ±90° 垂直候选，覆盖双垂直面单或长短边猜反。
        """
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        # 膨胀合并条空 → 近似实心码块，轮廓才稳
        k = cv2.getStructuringElement(cv2.MORPH_RECT, (21, 21))
        closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, k)
        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        H, W = gray.shape[:2]
        min_area = max(400.0, H * W * 0.002)
        scored = []  # (score, phi, aspect)
        for c in contours:
            area = float(cv2.contourArea(c))
            if area < min_area:
                continue
            (_cx, _cy), (rw, rh), ang = cv2.minAreaRect(c)
            if min(rw, rh) < 8:
                continue
            aspect = max(rw, rh) / max(1.0, min(rw, rh))
            phi = (ang - 90.0) if rh > rw else float(ang)
            while phi > 90.0:
                phi -= 180.0
            while phi <= -90.0:
                phi += 180.0
            # 细长且面积大优先（1D 面单）；方形大块（QR）也保留
            scored.append((area * min(aspect, 4.0), phi, aspect))

        if not scored:
            return []
        scored.sort(key=lambda t: t[0], reverse=True)
        phi = scored[0][1]

        angles = [phi]
        perp = phi + _PERP_OFFSET if phi <= 0.0 else phi - _PERP_OFFSET
        while perp > 90.0:
            perp -= 180.0
        while perp <= -90.0:
            perp += 180.0
        if abs(perp - phi) > 1.0:
            angles.append(perp)
        return angles

    @staticmethod
    def _filter_codes(codes):
        """
        丢掉退化 / 码制形状不符的假阳性。

        - 任意码：面积过小或短边 < 4px → 丢
        - 1D（Code128/I25…）：短边/长边 > 0.55 视为近方形 → 丢
          （旋转条码经 Otsu 后 ZBar 常吐出假 I25，且尺寸接近方形）
        """
        if not codes:
            return codes
        out = []
        for code in codes:
            r = code.rect
            cw, ch = int(r.width), int(r.height)
            if cw * ch < 64 or min(cw, ch) < 4:
                continue
            ctype = (getattr(code, "type", "") or "").upper()
            if ctype in _1D_TYPES:
                short, long_ = min(cw, ch), max(cw, ch)
                # 双垂直面单检出框可能接近 0.55~0.6；方形假 I25 是 ~1.0
                if short / long_ > 0.70:
                    continue
            out.append(code)
        return out

    def _map_results_in_crop(self, codes, ang: int, crop_shape,
                             ox: int, oy: int, scale: float) -> List[Dict[str, Any]]:
        """把 crop（或旋转后 crop）上的解码结果映射回原图坐标。"""
        ch, cw = crop_shape[:2]
        results = self._codes_to_results(codes)
        for c in results:
            bx1, by1, bx2, by2 = c["bbox"]
            if ang:
                bx1, by1, bx2, by2 = self._unrotate_bbox(
                    [bx1, by1, bx2, by2], ang, cw, ch)
            c["bbox"] = [
                ox + int(bx1 / scale),
                oy + int(by1 / scale),
                ox + int(bx2 / scale),
                oy + int(by2 / scale),
            ]
        return results

    @staticmethod
    def _unrotate_bbox(bbox, ang: float, cw: int, ch: int):
        """把旋转图上的轴对齐框映射回旋转前 crop 坐标（取四角外接矩形）。"""
        x1, y1, x2, y2 = bbox
        corners = np.array(
            [[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
            dtype=np.float32,
        )
        # warpAffine 用 M = R(ang) 绕中心变换；逆映射用 R(-ang)
        Minv = cv2.getRotationMatrix2D((cw / 2.0, ch / 2.0), -float(ang), 1.0)
        ones = np.ones((4, 1), dtype=np.float32)
        back = (Minv @ np.hstack([corners, ones]).T).T
        bx1 = int(np.clip(back[:, 0].min(), 0, cw))
        by1 = int(np.clip(back[:, 1].min(), 0, ch))
        bx2 = int(np.clip(back[:, 0].max(), 0, cw))
        by2 = int(np.clip(back[:, 1].max(), 0, ch))
        if bx2 <= bx1:
            bx2 = min(cw, bx1 + 1)
        if by2 <= by1:
            by2 = min(ch, by1 + 1)
        return [bx1, by1, bx2, by2]

    def _try_levels(self, gray: np.ndarray):
        """整帧/裁剪图通用的 4 级解码，成功即返回（已滤退化假阳性）。"""
        # 级别1：灰度 + CLAHE
        codes = self._filter_codes(_zbar_decode(self._clahe.apply(gray)))
        if not codes:
            # 级别2：2x 放大
            up = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            codes = self._filter_codes(_zbar_decode(self._clahe.apply(up)))
            if codes:
                codes = self._scale_codes(codes, 0.5)
        if not codes:
            # 级别3：Otsu 二值化
            _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            codes = self._filter_codes(_zbar_decode(binary))
        if not codes:
            # 级别4：锐化 + 二值化
            blur = cv2.GaussianBlur(gray, (0, 0), 3)
            sharp = cv2.addWeighted(gray, 1.5, blur, -0.5, 0)
            _, binary = cv2.threshold(sharp, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            codes = self._filter_codes(_zbar_decode(binary))
        return codes

    # ------------------------------------------------------------------
    # 轮廓矫正兜底（rectify fallback）
    # ------------------------------------------------------------------
    def _decode_rectify(self, gray: np.ndarray) -> List[Dict[str, Any]]:
        """
        主链全灭后的最后一级：轮廓矫正 + 静区留白再解。

        视图顺序：全帧 → 2x LANCZOS → 网格裁区；命中即返回（短路）。
        受 rectify_budget_ms 时间预算约束，坏帧超时放弃等下一帧。
        """
        deadline = None
        if self.rectify_budget_ms and self.rectify_budget_ms > 0:
            deadline = time.monotonic() + self.rectify_budget_ms / 1000.0
        H, W = gray.shape[:2]
        views = [(gray, 0, 0, 1.0)]
        for fx in (2, 3):
            up = cv2.resize(gray, None, fx=fx, fy=fx, interpolation=cv2.INTER_LANCZOS4)
            views.append((up, 0, 0, 1.0 / fx))
        for y0 in (0, H // 4, H // 2, H * 3 // 4, H // 6, H // 3):
            for x0 in (0, W // 4, W // 2, W * 3 // 4, W // 6, W // 3):
                views.append((gray[y0:y0 + 2 * H // 3, x0:x0 + 2 * W // 3],
                              x0, y0, 1.0))
        # 跨配置投票：同一内容在 ≥2 个不同矫正配置下出现才短路返回。
        # 单次出现的多为坏帧伪码（实测伪码常只在一两个配置里蹦），
        # 真码在多档 margin/尺寸/CLAHE 下会重复命中——投票既提速又抑伪。
        tallies = {}  # content -> [count, [mapped_results]]
        for view, ox, oy, scale in views:
            if min(view.shape) < 90 or view.shape[1] > 2600:
                continue
            stop = self._rectify_view(view, deadline, ox, oy, scale,
                                      gray.shape[:2], tallies)
            if stop:
                break
            if deadline and time.monotonic() > deadline:
                break
        if not tallies:
            return []
        best = max(tallies.values(), key=lambda kv: kv[0])
        return best[1]

    def _rectify_view(self, view: np.ndarray, deadline, ox: int, oy: int,
                      scale: float, gray_shape, tallies: dict) -> bool:
        """
        在一个视图上做轮廓矫正扫描，把命中记入 tallies。
        返回 True 表示应停止（投票达标或超预算）。
        """
        quads = []
        for mode in (cv2.THRESH_BINARY_INV, cv2.THRESH_BINARY):
            _, th = cv2.threshold(view, 0, 255, mode + cv2.THRESH_OTSU)
            for kern in _RECTIFY_KERNELS:
                closed = cv2.morphologyEx(th, cv2.MORPH_CLOSE, np.ones(kern, np.uint8))
                cnts, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL,
                                           cv2.CHAIN_APPROX_SIMPLE)
                for c in sorted(cnts, key=cv2.contourArea,
                                reverse=True)[:_RECTIFY_TOP_CONTOURS]:
                    if cv2.contourArea(c) < 1000:
                        continue
                    box = cv2.boxPoints(cv2.minAreaRect(c)).astype(np.float32)
                    cx, cy = box[:, 0].mean(), box[:, 1].mean()
                    # 极角排序四角 + 左上角起转：y+x 启发式在 83° 矩形会翻转，极角稳
                    quad = box[np.argsort(np.arctan2(box[:, 1] - cy, box[:, 0] - cx))]
                    k = int(np.argmin(quad[:, 0] + quad[:, 1]))
                    quads.append(np.roll(quad, -k, axis=0))
        if not quads:
            return False
        _, th_bin = cv2.threshold(view, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        for quad in quads:
            for margin in _RECTIFY_MARGINS:
                for Wp, Hp in _RECTIFY_SIZES:
                    if deadline and time.monotonic() > deadline:
                        return True
                    # 静区留白：quad 映射到画布内缩区，四周留白边
                    dst = np.array(
                        [[margin * Wp, margin * Hp],
                         [(1 - margin) * Wp, margin * Hp],
                         [(1 - margin) * Wp, (1 - margin) * Hp],
                         [margin * Wp, (1 - margin) * Hp]], np.float32)
                    M = cv2.getPerspectiveTransform(quad, dst)
                    Minv = cv2.getPerspectiveTransform(dst, quad)
                    codes = []
                    # 灰度矫正
                    warp = cv2.warpPerspective(view, M, (Wp, Hp),
                                               flags=cv2.INTER_CUBIC, borderValue=255)
                    codes = self._decode_warp(warp)
                    # 先二值化再矫正：边缘更锐，放大不糊
                    if not codes:
                        bwarp = cv2.warpPerspective(th_bin, M, (Wp, Hp),
                                                    flags=cv2.INTER_NEAREST, borderValue=255)
                        for fx in (2, 4):
                            up = cv2.resize(bwarp, None, fx=fx, fy=fx,
                                            interpolation=cv2.INTER_NEAREST)
                            for img in (up, 255 - up):
                                codes = self._rectify_filter(_zbar_decode(img))
                                if codes:
                                    break
                            if codes:
                                break
                    if codes:
                        self._tally_codes(codes, Minv, view.shape[:2],
                                          ox, oy, scale, gray_shape, tallies)
                        if max(v[0] for v in tallies.values()) >= 2:
                            return True
        return False

    @staticmethod
    def _tally_codes(codes, Minv, view_shape, ox: int, oy: int,
                     scale: float, gray_shape, tallies: dict):
        """把一个矫正命中的结果映回原图并计入投票（同内容同批只计一次）。"""
        mapped = QRDecoder._map_warp_codes(codes, Minv, view_shape,
                                           ox, oy, scale, gray_shape)
        seen = set()
        for res in mapped:
            content = res["content"]
            if content in seen:
                continue
            seen.add(content)
            if content in tallies:
                tallies[content][0] += 1
            else:
                tallies[content] = [1, [res]]

    @staticmethod
    def _rectify_filter(codes):
        """
        矫正路径专用过滤：只丢空内容/退化框，不做 1D 长宽比规则。

        长宽比规则（近方形丢弃）是为整帧路径的假 I25 设计的；矫正画布上
        符号几何已归一，实测该规则会误杀真码，且用户已拍板坏帧伪码风险
        由下游兜底（方案 B）。
        """
        if not codes:
            return codes
        out = []
        for code in codes:
            r = code.rect
            cw, ch = int(r.width), int(r.height)
            if cw * ch < 64 or min(cw, ch) < 4:
                continue
            content = code.data.decode("utf-8", errors="replace")
            if len(content) < 4:
                continue
            out.append(code)
        return out

    def _decode_warp(self, warp: np.ndarray):
        """矫正画布上的多级解码：CLAHE×3 → Otsu×2 → 2/4x NN，正反极性。"""
        for cl_v in _RECTIFY_CLAHE:
            enh = cv2.createCLAHE(cl_v, (8, 8)).apply(warp)
            for mode in (cv2.THRESH_BINARY, cv2.THRESH_BINARY_INV):
                _, b2 = cv2.threshold(enh, 0, 255, mode + cv2.THRESH_OTSU)
                for fx in (2, 4):
                    up = cv2.resize(b2, None, fx=fx, fy=fx,
                                    interpolation=cv2.INTER_NEAREST)
                    for img in (up, 255 - up):
                        codes = self._rectify_filter(_zbar_decode(img))
                        if codes:
                            return codes
            for img in (enh, 255 - enh):
                codes = self._rectify_filter(_zbar_decode(img))
                if codes:
                    return codes
        for img in (warp, 255 - warp):
            codes = self._rectify_filter(_zbar_decode(img))
            if codes:
                return codes
        return []

    @staticmethod
    def _map_warp_codes(codes, Minv, view_shape, ox: int, oy: int,
                        scale: float, gray_shape) -> List[Dict[str, Any]]:
        """把矫正画布上的结果经 Minv 映回视图，再映回原灰度图坐标。"""
        results = QRDecoder._codes_to_results(codes)
        vh, vw = view_shape[:2]
        gh, gw = gray_shape[:2]
        for r in results:
            x1, y1, x2, y2 = r["bbox"]
            corners = np.array(
                [[[x1, y1], [x2, y1], [x2, y2], [x1, y2]]], dtype=np.float32)
            back = cv2.perspectiveTransform(corners, Minv)[0]
            vx1 = float(np.clip(back[:, 0].min(), 0, vw))
            vy1 = float(np.clip(back[:, 1].min(), 0, vh))
            vx2 = float(np.clip(back[:, 0].max(), 0, vw))
            vy2 = float(np.clip(back[:, 1].max(), 0, vh))
            if vx2 <= vx1:
                vx2 = min(vw, vx1 + 1)
            if vy2 <= vy1:
                vy2 = min(vh, vy1 + 1)
            r["bbox"] = [
                int(np.clip(ox + vx1 / scale, 0, gw)),
                int(np.clip(oy + vy1 / scale, 0, gh)),
                int(np.clip(ox + vx2 / scale, 0, gw)),
                int(np.clip(oy + vy2 / scale, 0, gh)),
            ]
        return results

    @staticmethod
    def _codes_to_results(codes) -> List[Dict[str, Any]]:
        """把 pyzbar Decoded 列表转成统一的字典列表。"""
        results = []
        for code in codes:
            # 用属性访问：兼容 pyzbar Rect 与本文件 _ScaledRect（后者不可解包）
            x = int(code.rect.left)
            y = int(code.rect.top)
            w = int(code.rect.width)
            h = int(code.rect.height)
            content = code.data.decode("utf-8", errors="replace")
            results.append({
                "bbox": [x, y, x + w, y + h],  # [x1, y1, x2, y2]
                "content": content,
                "type": code.type,
            })
        return results

    @staticmethod
    def _scale_codes(codes, scale: float):
        """把 pyzbar 结果的坐标按 scale 缩放（用于放大解码后映射回原图）。"""
        class _ScaledRect:
            def __init__(self, r, s):
                self.left = int(r.left * s)
                self.top = int(r.top * s)
                self.width = max(1, int(r.width * s))
                self.height = max(1, int(r.height * s))

            def __iter__(self):
                # 兼容曾写出的 x, y, w, h = code.rect 解包写法
                yield self.left
                yield self.top
                yield self.width
                yield self.height

        class _ScaledCode:
            def __init__(self, c, s):
                self.rect = _ScaledRect(c.rect, s)
                self.data = c.data
                self.type = c.type

        return [_ScaledCode(c, scale) for c in codes]


    # ------------------------------------------------------------------
    # v1.2 跨帧渐进式搜索（slice_ms>0 时启用；思路见 tests/slice_decoder.py
    # 的验证记录——离线回归 9/14 与 v1.1 逐帧一致，单次最坏 1082ms vs 6100ms）
    # ------------------------------------------------------------------
    def _update_gate(self, gray) -> bool:
        """更新指纹参考帧；返回 True = 画面相对上帧发生了变化。"""
        fp = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)
        changed = True
        if self._fp_ref is not None:
            delta = float(np.mean(
                np.abs(fp.astype(np.int16) - self._fp_ref.astype(np.int16))))
            changed = delta > self.fp_thresh
        self._fp_ref = fp
        return changed

    def _reset_search(self):
        """清游标/票/完成标记（换物体、成功交付后调用）。"""
        self._cursor = None
        self._inner = None
        self._tallies_px = {}
        self._search_done = False
        self._quick_tried = False  # 新场景重新跑一次快速主链

    def _decode_progressive(self, gray) -> List[Dict[str, Any]]:
        """
        指纹门 + 静态跳重复主链 + 时间片兜底。返回同 decode()。

        三态门：画面变了→清零重搜；没变+已搜完→跳过；没变+没搜完→续搜。
        """
        scene_changed = self._update_gate(gray)
        if scene_changed:
            self._reset_search()      # 画面变了：进度作废，本帧就重搜

        # 同画面主链刚失败过，重复跑纯浪费（难帧主链 ~1.4s/次）
        if not self._quick_tried:
            codes, ang = self._try_levels_with_rotation(gray)
            self._quick_tried = True
            if codes:
                self._reset_search()
                return self._map_results_in_crop(codes, ang, gray.shape[:2],
                                                 ox=0, oy=0, scale=1.0)

        if self._search_done:
            return []   # 静态 + 整个搜索空间已榨干：跳过兜底
        return self._decode_rectify_sliced(gray)

    def _decode_rectify_sliced(self, gray) -> List[Dict[str, Any]]:
        """
        时间片版的轮廓矫正兜底：视图顺序与 v1.1 完全一致（全帧 → 2x/3x
        LANCZOS → 36 裁区），但从游标续搜，超时存档返回而非丢弃进度。
        命中 ≥2 票短路；整个搜索空间走完才交付 1 票结果（等价 v1.1 跑满预算）。
        """
        deadline = time.monotonic() + self.slice_ms / 1000.0
        H, W = gray.shape[:2]
        views = [(gray, 0, 0, 1.0)]
        for fx in (2, 3):
            up = cv2.resize(gray, None, fx=fx, fy=fx,
                            interpolation=cv2.INTER_LANCZOS4)
            views.append((up, 0, 0, 1.0 / fx))
        for y0 in (0, H // 4, H // 2, H * 3 // 4, H // 6, H // 3):
            for x0 in (0, W // 4, W // 2, W * 3 // 4, W // 6, W // 3):
                views.append((gray[y0:y0 + 2 * H // 3, x0:x0 + 2 * W // 3],
                              x0, y0, 1.0))

        start = self._cursor if self._cursor is not None else 0
        for i in range(start, len(views)):
            view, ox, oy, scale = views[i]
            if min(view.shape) < 90 or view.shape[1] > 2600:
                continue
            self._cursor = i  # 当前卡在第 i 个视图（超时也留在这里）
            reason = self._rectify_view_sliced(
                view, deadline, ox, oy, scale, gray.shape[:2],
                self._tallies_px, self._inner if i == start else None)
            if reason == "vote":
                best = max(self._tallies_px.values(), key=lambda kv: kv[0])
                self._reset_search()
                return best[1]
            if reason == "timeout":
                return []      # 时间片到：游标已存，下帧续
            self._cursor = i + 1
            self._inner = None
            if deadline and time.monotonic() > deadline:
                return []

        # 整个搜索空间走完（v1.1 要烧满 5s 预算，这里靠跨帧累计且不冻屏）
        self._search_done = True
        self._cursor = None
        self._inner = None
        if not self._tallies_px:
            return []
        best = max(self._tallies_px.values(), key=lambda kv: kv[0])
        result = best[1]
        self._tallies_px = {}  # 已交付 → 清票，下一个物体从头开始
        return result

    def _rectify_view_sliced(self, view, deadline, ox, oy, scale,
                             gray_shape, tallies, inner_start):
        """
        v1.1 _rectify_view 的断点续搜版：游标 (qi, mi, si, st)——st=项目内
        原子阶段号（单次 zbar）。时间片中断只存"下一个未跑阶段"，任何配置
        都不会被截断丢失。

        返回："vote"（≥2 票）| "timeout"（游标已存）| "done"（本视图搜完）
        """
        if deadline and time.monotonic() > deadline:
            self._inner = inner_start or (0, 0, 0, 0)
            return "timeout"

        # 找轮廓四角（与 v1.1 一致；面积降序，静止画面下顺序稳定）
        quads = []
        for mode in (cv2.THRESH_BINARY_INV, cv2.THRESH_BINARY):
            _, th = cv2.threshold(view, 0, 255, mode + cv2.THRESH_OTSU)
            for kern in _RECTIFY_KERNELS:
                closed = cv2.morphologyEx(th, cv2.MORPH_CLOSE,
                                          np.ones(kern, np.uint8))
                cnts, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL,
                                           cv2.CHAIN_APPROX_SIMPLE)
                for c in sorted(cnts, key=cv2.contourArea,
                                reverse=True)[:_RECTIFY_TOP_CONTOURS]:
                    if cv2.contourArea(c) < 1000:
                        continue
                    box = cv2.boxPoints(cv2.minAreaRect(c)).astype(np.float32)
                    cx, cy = box[:, 0].mean(), box[:, 1].mean()
                    quad = box[np.argsort(
                        np.arctan2(box[:, 1] - cy, box[:, 0] - cx))]
                    k = int(np.argmin(quad[:, 0] + quad[:, 1]))
                    quads.append(np.roll(quad, -k, axis=0))
        if not quads:
            return "done"
        if deadline and time.monotonic() > deadline:
            self._inner = (0, 0, 0, 0)
            return "timeout"

        _, th_bin = cv2.threshold(view, 0, 255,
                                  cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        qi0, mi0, si0, st0 = inner_start or (0, 0, 0, 0)
        if qi0 >= len(quads):
            return "done"

        for qi in range(qi0, len(quads)):
            quad = quads[qi]
            m_start = mi0 if qi == qi0 else 0
            for mi in range(m_start, len(_RECTIFY_MARGINS)):
                margin = _RECTIFY_MARGINS[mi]
                s_start = si0 if (qi == qi0 and mi == mi0) else 0
                for si in range(s_start, len(_RECTIFY_SIZES)):
                    # 只有上次中断的确切项目带阶段断点，其余从阶段 0 跑
                    st = st0 if (qi == qi0 and mi == mi0 and si == si0) else 0
                    if deadline and time.monotonic() > deadline:
                        self._inner = (qi, mi, si, st)
                        return "timeout"
                    Wp, Hp = _RECTIFY_SIZES[si]
                    dst = np.array(
                        [[margin * Wp, margin * Hp],
                         [(1 - margin) * Wp, margin * Hp],
                         [(1 - margin) * Wp, (1 - margin) * Hp],
                         [margin * Wp, (1 - margin) * Hp]], np.float32)
                    M = cv2.getPerspectiveTransform(quad, dst)
                    Minv = cv2.getPerspectiveTransform(dst, quad)
                    warp = cv2.warpPerspective(view, M, (Wp, Hp),
                                               flags=cv2.INTER_CUBIC,
                                               borderValue=255)
                    codes = []
                    enh_cache = {}
                    # warp 管线（32 个原子阶段，逐阶段续）
                    while st < _WARP_STAGES:
                        if deadline and time.monotonic() > deadline:
                            self._inner = (qi, mi, si, st)
                            return "timeout"
                        codes = self._rectify_filter(
                            self._warp_stage(warp, st, enh_cache))
                        if codes:
                            break
                        st += 1
                    # 二值路径（4 个原子阶段）——先二值化再矫正，边缘更锐
                    if not codes:
                        bwarp = None
                        while st < _WARP_STAGES + _BIN_STAGES:
                            if deadline and time.monotonic() > deadline:
                                self._inner = (qi, mi, si, st)
                                return "timeout"
                            if bwarp is None:
                                bwarp = cv2.warpPerspective(
                                    th_bin, M, (Wp, Hp),
                                    flags=cv2.INTER_NEAREST, borderValue=255)
                            codes = self._rectify_filter(
                                self._bin_stage(bwarp, st - _WARP_STAGES))
                            if codes:
                                break
                            st += 1
                    if codes:
                        self._tally_codes(codes, Minv, view.shape[:2],
                                          ox, oy, scale, gray_shape, tallies)
                        if max(v[0] for v in tallies.values()) >= 2:
                            return "vote"
        return "done"

    @staticmethod
    def _warp_stage(warp, st, enh_cache):
        """
        warp 管线第 st 个原子阶段 = 恰一次 pyzbar 调用（与 v1.1 _decode_warp
        完全同序）：3 CLAHE 档 × (2极性 × 2放大 × 2正反 + 2原图正反) + 2 原图兜底。
        """
        if st >= _WARP_STAGES:
            return []
        if st < 30:
            cl_i = st // 10
            if cl_i not in enh_cache:
                enh_cache[cl_i] = cv2.createCLAHE(
                    _RECTIFY_CLAHE[cl_i], (8, 8)).apply(warp)
            enh = enh_cache[cl_i]
            sub = st % 10
            if sub < 8:
                mode = cv2.THRESH_BINARY if sub < 4 else cv2.THRESH_BINARY_INV
                k = sub % 4
                _, b2 = cv2.threshold(enh, 0, 255, mode + cv2.THRESH_OTSU)
                fx = 2 if k < 2 else 4
                up = cv2.resize(b2, None, fx=fx, fy=fx,
                                interpolation=cv2.INTER_NEAREST)
                img = up if k % 2 == 0 else 255 - up
                return _zbar_decode(img)
            img = enh if sub == 8 else 255 - enh
            return _zbar_decode(img)
        img = warp if st == 30 else 255 - warp
        return _zbar_decode(img)

    @staticmethod
    def _bin_stage(bwarp, k):
        """二值路径第 k 个原子阶段（0..3）：放大 2/4 × 正反，各一次 zbar。"""
        fx = 2 if k < 2 else 4
        up = cv2.resize(bwarp, None, fx=fx, fy=fx,
                        interpolation=cv2.INTER_NEAREST)
        img = up if k % 2 == 0 else 255 - up
        return _zbar_decode(img)


def match_qr_to_detections(qr_results: List[Dict[str, Any]],
                           yolo_detections: List[Dict[str, Any]]) -> Dict[int, str]:
    """
    将二维码与 YOLO 检测框进行空间匹配

    用途：
        在产线控制中，YOLO 负责检测物体，二维码负责识别物体信息。
        本函数将二维码内容绑定到对应的 YOLO 检测框，实现"这个二维码属于这个物体"的关联。

    参数：
        qr_results: List[Dict[str, Any]] 类型
                    - QRDecoder.decode() 的返回值
                    - 每个字典包含二维码的 bbox、content、type

        yolo_detections: List[Dict[str, Any]] 类型
                        - YOLO 检测器的返回值
                        - 每个字典包含检测框的 bbox、class、confidence 等

    返回：Dict[int, str] 类型
        - 键：YOLO 检测框的索引（从 0 开始）
        - 值：二维码的内容字符串
        - 示例：{0: "产品A", 2: "批次123"} 表示第0个检测框包含"产品A"的二维码，
                第2个检测框包含"批次123"的二维码，第1个检测框没有二维码

    匹配逻辑：
        1. 遍历每个二维码
        2. 计算二维码的中心点坐标
        3. 检查中心点是否落在某个 YOLO 检测框内
        4. 如果落在框内，建立绑定关系
        5. 一个二维码只绑定一个框（第一个匹配的框）

    注意：
        - 一个二维码只能绑定到一个 YOLO 框（即使中心点落在多个框的重叠区域）
        - 一个 YOLO 框可以被多个二维码绑定（如果多个二维码都在这个框内）
        - 返回的字典可能不包含所有 YOLO 框的索引（没有二维码的框不会出现）
    """
    bindings = {}  # 存储绑定结果：{YOLO框索引: 二维码内容}

    # 遍历每个二维码
    for qr in qr_results:
        # 获取二维码的边界框坐标
        qr_bbox = qr.get("bbox", [])

        # 检查边界框格式是否正确（必须有4个值）
        if len(qr_bbox) != 4:
            continue  # 跳过格式错误的二维码

        # 计算二维码的中心点坐标
        # cx = (左边界 + 右边界) / 2
        # cy = (上边界 + 下边界) / 2
        cx = (qr_bbox[0] + qr_bbox[2]) / 2
        cy = (qr_bbox[1] + qr_bbox[3]) / 2

        # 遍历每个 YOLO 检测框
        for i, det in enumerate(yolo_detections):
            # 获取 YOLO 检测框的边界框坐标
            det_bbox = det.get("bbox", [])

            # 检查边界框格式是否正确
            if len(det_bbox) != 4:
                continue  # 跳过格式错误的检测框

            # 解包 YOLO 框的四个坐标
            x1, y1, x2, y2 = det_bbox

            # 判断二维码中心点是否在 YOLO 框内
            # 条件：中心点的 x 坐标在 [x1, x2] 之间，y 坐标在 [y1, y2] 之间
            if x1 <= cx <= x2 and y1 <= cy <= y2:
                # 匹配成功！将二维码内容绑定到这个 YOLO 框
                bindings[i] = qr.get("content", "")
                break  # 一个码只绑一个框，跳出内层循环

    return bindings  # 返回所有绑定关系
