#!/home/chow/桌面/9_15ubuntu/py/.venv/bin/python3
"""
纯扫码测试（无 YOLO / 无 Qt / 无 ROS / 无 MQTT）
==================================================
直接开摄像头，调 QRDecoder().decode(frame)——与产线同一条解码链
（直读 4 级 → minAreaRect 估角 → 90° 兜底 → 轮廓矫正兜底+投票，见
qr_decoder.py 版本历史），测当前基线。

用法（不需要 source，shebang 直用 venv 解释器）：
    ./scan_test.py                      # 摄像头 0，每帧扫（生产默认：渐进式搜索）
    ./scan_test.py --cam 1              # 指定摄像头
    ./scan_test.py --size 1280x720      # 指定分辨率
    ./scan_test.py --interval 3         # 每 3 帧扫一次（停带扫码推荐）
    ./scan_test.py --interval 15        # 每 15 帧扫一次（皮带运行时看画面用）
    ./scan_test.py --slice 300          # 时间片调小 → 更流畅、搜得更慢
    ./scan_test.py --slice 0 --budget 5000   # 回退旧行为（烧满预算、画面会冻）
    ./scan_test.py --slice 600 --fp-thresh 6   # 指纹门调钝（画面抖动被判"动了"时用）

卡顿说明：生产默认已是渐进式搜索（slice=600）——解不出的帧最多占 0.6s，
存档游标后下一帧继续搜，退出统计里"预览最大间隔"就是冻结指标（应 ≤1.1s）。
退回旧行为（--slice 0）时解不出的帧会烧满 --budget（默认 5000ms），
解码期间画面定格 5~6 秒——这是改动前的行为。
不指定 --slice 时用生产默认 _DEFAULT_SLICE_MS（当前 600）；--budget 仅
在 --slice 0 时生效。

手动画框模式（模拟"已知码的精确位置"，测相机像素极限）：
    鼠标左键拖拽 = 画一个框（把条码框进去，类似微信扫码框）
    框的四角/内部按住拖动 = 调大小/移动；右键 = 清除框
    有框时：每帧只解框内区域（直读+估角）+ 把框当码的四角透视摆正解码
    ——不走 6000 组合兜底，所以不卡；这就是"未来定位问题解决后"的解码路径。
    框越小 = 码越远：逐渐拉小框/推远条码，框上会实时显示 WxH 像素，
    解不出的最小框尺寸 ≈ 这台相机+这条码的最大扫码距离极限。

按键：
    空格   暂停 / 继续（定格细看解码框）
    s      保存当前帧到 py/scan_test_out/（扫不出来的瞬间按它取证）
    q/ESC  退出并打印统计

退出统计：总帧、成功帧、成功率、平均/最坏耗时、各内容命中次数、
成功时的最小框尺寸（像素）。
"""
import argparse
import os
import sys
import time
from collections import Counter
from datetime import datetime

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vision.qr_decoder import QRDecoder, __version__  # noqa: E402

SAVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_test_out")


def parse_args():
    p = argparse.ArgumentParser(description="纯扫码测试（与产线同链路）")
    p.add_argument("--cam", type=int, default=0, help="摄像头索引（默认 0）")
    p.add_argument("--size", type=str, default=None,
                   help="分辨率 WxH，如 640x480（默认用相机原生）")
    p.add_argument("--interval", type=int, default=1,
                   help="扫码间隔（帧）：每 N 帧解码一次，默认 1=每帧都扫")
    p.add_argument("--budget", type=int, default=5000,
                   help="轮廓矫正兜底时间预算（毫秒），默认 5000=生产值；"
                        "1=基本关兜底（只测主链速度）")
    p.add_argument("--slice", type=int, default=None,
                   help="v1.2 时间片（毫秒）：不传=跟随生产默认 600；"
                        ">0=渐进式搜索（时间片+游标续搜+帧指纹门）；"
                        "0=强制旧行为（单次烧满 --budget）")
    p.add_argument("--fp-thresh", type=float, default=4.0,
                   help="v1.2 指纹门阈值（32×32 灰度平均绝对差），"
                        "默认 4.0，仅 --slice 模式生效")
    return p.parse_args()


# ---------------------------------------------------------------------------
# 手动画框模式（模拟"已知码精确位置"）
# ---------------------------------------------------------------------------
# sel 状态字典：box=(x1,y1,x2,y2)|None, drag=拖拽状态, shape=(H,W), win=窗口名
def _img_xy(sel, mx, my):
    """鼠标窗口坐标 → 图像坐标（WINDOW_NORMAL 缩放窗口下必须做映射）。"""
    H, W = sel["shape"]
    try:
        ix, iy, iw, ih = cv2.getWindowImageRect(sel["win"])
    except cv2.error:
        ix, iy, iw, ih = 0, 0, 0, 0
    if iw <= 0 or ih <= 0:
        ix, iy, iw, ih = 0, 0, W, H
    x = int(round((mx - ix) * W / iw))
    y = int(round((my - iy) * H / ih))
    return max(0, min(W - 1, x)), max(0, min(H - 1, y))


def _norm_box(a, b, min_sz=20):
    x1, x2 = sorted((a[0], b[0]))
    y1, y2 = sorted((a[1], b[1]))
    if x2 - x1 < min_sz:
        x2 = min(x1 + min_sz, x2 + 200)
    if y2 - y1 < min_sz:
        y2 = min(y1 + min_sz, y2 + 200)
    return (x1, y1, x2, y2)


def _corner_hit(box, x, y, th=15):
    x1, y1, x2, y2 = box
    for name, (cx, cy) in (("nw", (x1, y1)), ("ne", (x2, y1)),
                           ("sw", (x1, y2)), ("se", (x2, y2))):
        if abs(x - cx) <= th and abs(y - cy) <= th:
            return name
    return None


def on_mouse(event, x, y, flags, param):
    """左键拖拽画框/四角缩放/框内移动，右键清除。"""
    x, y = _img_xy(param, x, y)
    box = param["box"]
    if event == cv2.EVENT_RBUTTONDOWN:
        param["box"] = None
        param["drag"] = None
        return
    if event == cv2.EVENT_LBUTTONDOWN:
        if box is not None:
            c = _corner_hit(box, x, y)
            if c:
                param["drag"] = ("resize", c, box)
                return
            if box[0] <= x <= box[2] and box[1] <= y <= box[3]:
                param["drag"] = ("move", x - box[0], y - box[1], box)
                return
        param["drag"] = ("new", (x, y))
        param["box"] = (x, y, x, y)
    elif event == cv2.EVENT_MOUSEMOVE and param["drag"]:
        mode = param["drag"][0]
        if mode == "new":
            param["box"] = _norm_box(param["drag"][1], (x, y))
        elif mode == "move":
            _, dx, dy, ob = param["drag"]
            w, h = ob[2] - ob[0], ob[3] - ob[1]
            x1 = max(0, min(x - dx, param["shape"][1] - 1 - w))
            y1 = max(0, min(y - dy, param["shape"][0] - 1 - h))
            param["box"] = (x1, y1, x1 + w, y1 + h)
        else:  # resize
            _, c, ob = param["drag"]
            x1, y1, x2, y2 = ob
            if c in ("nw", "sw"):
                x1 = min(x, x2 - 20)
            else:
                x2 = max(x, x1 + 20)
            if c in ("nw", "ne"):
                y1 = min(y, y2 - 20)
            else:
                y2 = max(y, y1 + 20)
            param["box"] = (x1, y1, x2, y2)
    elif event == cv2.EVENT_LBUTTONUP:
        param["drag"] = None
        if param["box"] is not None:
            x1, y1, x2, y2 = param["box"]
            if x2 - x1 < 20 or y2 - y1 < 20:
                param["box"] = None


def _decode_warp_lean(decoder, warp):
    """
    框模式专用的精简解码链（区别于暴力枚举用的 _decode_warp）：
    CLAHE+Otsu 原尺寸 + 小图才 2×，正反极性，最多约 6 次 zbar。
    失败帧耗时被限制在 ~150ms，画面不会被拖死。
    """
    from pyzbar import pyzbar
    h, w = warp.shape[:2]
    enh = cv2.createCLAHE(3.0, (8, 8)).apply(warp)
    _, binary = cv2.threshold(enh, 0, 255,
                              cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    imgs = [enh, binary]
    if max(w, h) <= 500:  # 小框才值得放大（大框像素已够，放大纯烧 CPU）
        imgs.append(cv2.resize(binary, None, fx=2, fy=2,
                               interpolation=cv2.INTER_NEAREST))
    for im in imgs:
        for img in (im, 255 - im):
            codes = decoder._rectify_filter(pyzbar.decode(img))
            if codes:
                return codes
    return []


def decode_in_box(decoder, frame, box):
    """
    在已知四角的前提下解框内条码——模拟"未来定位问题解决后"的路径：
    1) 框内裁剪直读+估角（码在框里可能还是歪的，先按老办法转一下）
    2) 把框当成码的精确四角，留静区透视摆正 → 精简解码链
    都不走 6000 组合兜底 → 每帧几十~几百毫秒，不卡。
    返回 (results, 路径标签)。
    """
    if len(frame.shape) == 3:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    else:
        gray = frame
    x1, y1, x2, y2 = box
    crop = gray[y1:y2, x1:x2]
    if crop.size:
        codes, ang = decoder._try_levels_with_rotation(crop)
        if codes:
            results = decoder._map_results_in_crop(
                codes, ang, crop.shape[:2], ox=x1, oy=y1, scale=1.0)
            return results, "crop"

    # 框 = 精确四角 → 留静区摆正；画布贴着框的原生尺寸（不瞎放大）
    quad = np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], np.float32)
    bw, bh = max(1, x2 - x1), max(1, y2 - y1)
    Wp = int(min(720, max(240, bw)))
    Hp = int(min(540, max(150, bh * Wp / bw)))
    for margin in (0.10, 0.04):
        dst = np.array([[margin * Wp, margin * Hp],
                        [(1 - margin) * Wp, margin * Hp],
                        [(1 - margin) * Wp, (1 - margin) * Hp],
                        [margin * Wp, (1 - margin) * Hp]], np.float32)
        M = cv2.getPerspectiveTransform(quad, dst)
        Minv = cv2.getPerspectiveTransform(dst, quad)
        warp = cv2.warpPerspective(gray, M, (Wp, Hp),
                                   flags=cv2.INTER_CUBIC, borderValue=255)
        codes = _decode_warp_lean(decoder, warp)
        if codes:
            results = decoder._map_warp_codes(codes, Minv, gray.shape[:2],
                                              ox=0, oy=0, scale=1.0,
                                              gray_shape=gray.shape[:2])
            return results, "warp"
    return [], ""


def draw_overlay(frame, results, fps, last_ms, stats, sel=None,
                 box_msg=None, box_ok=False):
    """画解码框 + 手动画框 + 性能信息。"""
    for r in results:
        x1, y1, x2, y2 = r["bbox"]
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
        label = f'{r["content"][:32]} [{r["type"]}]'
        cv2.putText(frame, label, (x1, max(20, y1 - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

    if sel is not None and sel.get("box"):
        x1, y1, x2, y2 = sel["box"]
        color = (0, 255, 255)  # 黄：框本体
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        for cx, cy in ((x1, y1), (x2, y1), (x1, y2), (x2, y2)):
            cv2.rectangle(frame, (cx - 5, cy - 5), (cx + 5, cy + 5),
                          (255, 128, 0), -1)  # 橙：四角把手
        cv2.putText(frame, f"{x2 - x1}x{y2 - y1}px", (x1, max(14, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2)

    lines = [
        f"decode: {last_ms:.1f} ms   fps: {fps:.1f}   "
        f"run: {int(time.perf_counter() - t_start)}s",
        f"ok: {stats['ok']}/{stats['total']} "
        f"({100.0 * stats['ok'] / max(1, stats['total']):.0f}%)",
        "[drag] box  [right-click] clear  [space] pause  [s] save  [q] quit",
    ]
    if box_msg is not None:
        lines.insert(0, f"BOX {'OK: ' + box_msg if box_ok else 'FAIL'}")
    for i, line in enumerate(lines):
        if i == 0 and box_msg is not None:
            color = (0, 255, 0) if box_ok else (0, 0, 255)
        else:
            color = (0, 200, 255)
        cv2.putText(frame, line, (8, 24 + i * 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
    return frame


def main():
    args = parse_args()
    os.makedirs(SAVE_DIR, exist_ok=True)

    decoder = QRDecoder(slice_ms=args.slice,
                    fp_thresh=args.fp_thresh)
    if not decoder.available:
        print("pyzbar 不可用，请检查 venv 依赖", file=sys.stderr)
        return 1
    if decoder.slice_ms > 0:
        src = "（不传，跟随生产默认）" if args.slice is None else ""
        mode_desc = (f"v1.2 渐进式搜索 时间片 {decoder.slice_ms}ms "
                     f"指纹门 {args.fp_thresh}{src}")
    else:
        decoder.rectify_budget_ms = args.budget
        mode_desc = f"旧行为（v1.1）单次兜底预算 {args.budget}ms"

    cap = cv2.VideoCapture(args.cam)
    if not cap.isOpened():
        print(f"无法打开摄像头 {args.cam}", file=sys.stderr)
        return 1
    if args.size:
        w, h = (int(v) for v in args.size.lower().split("x"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
    got_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    got_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"QRDecoder v{__version__}  摄像头 {args.cam} 已打开 {got_w}x{got_h}，"
          f"扫码间隔：每 {args.interval} 帧，{mode_desc}")

    win = "scan_test"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)

    sel = {"box": None, "drag": None, "shape": (got_h, got_w), "win": win}
    cv2.setMouseCallback(win, on_mouse, sel)

    stats = {"total": 0, "ok": 0}
    content_hits = Counter()
    ok_sizes = []           # 成功解码时的框尺寸 (w,h)，退出时取最小=最远距离
    times = []
    last_ms = 0.0
    fps = 0.0
    max_gap = 0.0             # 相邻两次循环间隔（预览冻结代理指标）
    paused = False
    pending = None          # 暂停期间按 s 时待保存的帧
    last_frame = None
    prev_t = time.perf_counter()
    t_start = time.perf_counter()   # 运行起点（叠加显示运行时长）
    frame_idx = 0
    box_msg, box_ok = None, False

    while True:
        if not paused:
            ret, frame = cap.read()
            if not ret:
                print("读帧失败，退出", file=sys.stderr)
                break
            last_frame = frame
            sel["shape"] = frame.shape[:2]
            frame_idx += 1
            if sel["box"]:
                # 画框模式：只解框内（模拟已知位置），每帧都解，不卡
                t0 = time.perf_counter()
                results, via = decode_in_box(decoder, frame, sel["box"])
                last_ms = (time.perf_counter() - t0) * 1000
                times.append(last_ms)
                stats["total"] += 1
                box_ok = bool(results)
                box_msg = (",".join(r["content"] for r in results)[:40]
                           if results else "FAIL")
                if results:
                    stats["ok"] += 1
                    b = sel["box"]
                    ok_sizes.append((b[2] - b[0], b[3] - b[1]))
                    for r in results:
                        content_hits[r["content"]] += 1
            # 每 interval 帧才解码一次：其余帧直接跳过，画面不被解码阻塞
            elif frame_idx % args.interval != 0:
                results = []
                box_msg, box_ok = None, False
            else:
                t0 = time.perf_counter()
                results = decoder.decode(frame)
                last_ms = (time.perf_counter() - t0) * 1000
                times.append(last_ms)
                stats["total"] += 1
                box_msg, box_ok = None, False
                if results:
                    stats["ok"] += 1
                    for r in results:
                        content_hits[r["content"]] += 1
        else:
            results = []  # 暂停时不重复解码，只显示定格画面

        now = time.perf_counter()
        max_gap = max(max_gap, now - prev_t)
        fps = 0.9 * fps + 0.1 * (1.0 / max(1e-6, now - prev_t))
        prev_t = now

        show = draw_overlay(last_frame.copy(), results, fps, last_ms, stats,
                            sel=sel, box_msg=box_msg, box_ok=box_ok)
        if paused:
            cv2.putText(show, "PAUSED", (8, 100), cv2.FONT_HERSHEY_SIMPLEX,
                        1.0, (0, 0, 255), 3)
        cv2.imshow(win, show)

        key = cv2.waitKey(1) & 0xFF
        if key in (ord("q"), 27):
            break
        if key == ord(" "):
            paused = not paused
            if not paused:
                results = []
        elif key == ord("s") and last_frame is not None:
            path = os.path.join(
                SAVE_DIR, datetime.now().strftime("%H%M%S_%f") + ".png")
            cv2.imwrite(path, last_frame)
            print(f"已保存 {path}")

    cap.release()
    cv2.destroyAllWindows()

    print("\n===== 统计 =====")
    print(f"总帧: {stats['total']}  成功: {stats['ok']}  "
          f"成功率: {100.0 * stats['ok'] / max(1, stats['total']):.1f}%")
    if times:
        print(f"耗时: 平均 {sum(times) / len(times):.1f} ms  "
              f"最坏 {max(times):.1f} ms")
    if max_gap:
        print(f"预览最大间隔: {max_gap * 1000:.0f} ms"
              + ("（渐进式搜索：≤ 时间片+主链耗时）"
                 if decoder.slice_ms > 0 else ""))
    if decoder.slice_ms > 0:
        print(f"渐进式搜索状态: {decoder.progressive_state()}")
    if content_hits:
        print("内容命中:")
        for content, n in content_hits.most_common():
            print(f"  {n:>5} x  {content[:60]}")
    if ok_sizes:
        mw = min(ok_sizes, key=lambda s: s[0])
        mh = min(ok_sizes, key=lambda s: s[1])
        print(f"画框成功解码: {len(ok_sizes)} 次  "
              f"最小框宽 {mw[0]}px / 最小框高 {mh[1]}px"
              "（框越小=码越远，约等于最大测距极限）")
    if SAVE_DIR and os.listdir(SAVE_DIR):
        print(f"取证图目录: {SAVE_DIR}/ ({len(os.listdir(SAVE_DIR))} 张)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
