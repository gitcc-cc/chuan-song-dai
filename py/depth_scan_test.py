#!/home/chow/桌面/9_15ubuntu/py/.venv/bin/python3
"""
深度相机（HP60C）扫码测试
=========================
扫码测试目前只有 scan_test.py（OpenCV 索引摄像头），够不到 HP60C——它只发
ROS2 话题。本脚本专门测"深度相机画面 + 同一条解码链"的扫码表现：

    ROS2 订阅 RGB（可选深度） → QRDecoder.decode()（同产线，可开 v1.2）
    → 窗口显示 RGB + 解码框 + 码中心深度值(mm) → 退出打印统计

与产线完全同一份解码器（vision.qr_decoder.QRDecoder），不开 YOLO、不开 Qt。

启动（两个终端）：
    终端1: source /opt/ros/humble/setup.bash
           ros2 launch ascamera hp60c.launch.py        # 起相机节点
    终端2: source /opt/ros/humble/setup.bash
           ./depth_scan_test.py                # 必须先 source（rclpy 在系统侧）

用法（生产默认已是渐进式搜索 slice=600，不指定 --slice 即为新方法）：
    ./depth_scan_test.py                      # 渐进式搜索（时间片0.6s+游标+指纹门）
    ./depth_scan_test.py --interval 3         # 每 3 帧解一次（停带扫码推荐，画面更顺）
    ./depth_scan_test.py --slice 300          # 时间片调小 → 更流畅、搜得更慢
    ./depth_scan_test.py --slice 0 --budget 5000  # 回退旧行为（烧满预算、画面会冻）
    ./depth_scan_test.py --model best.pt      # 带 YOLO，码只解在检测框里（ROI 路径）
    ./depth_scan_test.py --no-depth           # 不订阅深度图，只测扫码

扫码要点（实测结论）：条码模块宽需 ≥6px 才扛得住工业现场模糊；
画面 640×480 下 EAN-13 约需 570px 宽、Code128(8字符) 约需 600px。
达不到就解不出，属画质门槛而非解码器问题。

按键：
    空格 暂停/继续（定格细看）  s 保存当前帧到 scan_test_out/  q/ESC 退出

退出统计：解码次数、成功率、平均/最坏耗时、预览最大间隔、内容命中、深度读数。
    注意"成功 N 次"才是准的——画面里的框只闪半秒就被下一帧刷掉。
"""
import argparse
import os
import sys
import threading
import time
from collections import Counter
from datetime import datetime

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests"))
from vision.qr_decoder import QRDecoder, __version__  # noqa: E402

SAVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_test_out")
RGB_TOPIC = "/ascamera_hp60c/camera_publisher/rgb0/image"
DEPTH_TOPIC = "/ascamera_hp60c/camera_publisher/depth0/image_raw"


def parse_args():
    p = argparse.ArgumentParser(description="深度相机扫码测试（HP60C）")
    p.add_argument("--slice", type=int, default=0,
                   help="v1.2 时间片（毫秒），>0 启用渐进式搜索，0=正式链")
    p.add_argument("--fp-thresh", type=float, default=4.0,
                   help="指纹门阈值（仅 --slice 生效），默认 4.0")
    p.add_argument("--interval", type=int, default=1,
                   help="扫码间隔（帧）：每 N 帧解码一次，默认 1")
    p.add_argument("--model", type=str, default=None,
                   help="可选：YOLO 权重路径，提供则码只解在检测框内（ROI 路径）")
    p.add_argument("--conf", type=float, default=0.5, help="YOLO 置信度（默认 0.5）")
    p.add_argument("--no-depth", action="store_true", help="不订阅深度图")
    return p.parse_args()


class RosCamera:
    """后台线程 spin ROS2，回调只做 cv_bridge 转换 + 帧缓存。"""

    def __init__(self, want_depth=True):
        import rclpy
        from rclpy.node import Node
        from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
        from cv_bridge import CvBridge

        self._rclpy = rclpy
        self._node = None
        self.lock = threading.Lock()
        self.rgb = None
        self.depth = None
        self.seq = 0
        self._want_depth = want_depth
        self._qos = QoSProfile(depth=1,
                               reliability=ReliabilityPolicy.BEST_EFFORT,
                               durability=DurabilityPolicy.VOLATILE)
        self._bridge_cls = CvBridge
        self._Node = Node

    def start(self):
        rclpy = self._rclpy
        if not rclpy.ok():
            rclpy.init()
        self._node = self._Node("depth_scan_test")

        def make_cb(store):
            def cb(msg):
                try:
                    img = (self._bridge_cls().imgmsg_to_cv2(msg, "bgr8")
                           if store == "rgb"
                           else self._bridge_cls().imgmsg_to_cv2(
                               msg, "passthrough"))
                    with self.lock:
                        setattr(self, store, img)
                        if store == "rgb":
                            self.seq += 1
                except Exception:
                    pass
            return cb

        self._node.create_subscription(
            _msg_image_cls(), RGB_TOPIC, make_cb("rgb"), self._qos)
        if self._want_depth:
            self._node.create_subscription(
                _msg_image_cls(), DEPTH_TOPIC, make_cb("depth"), self._qos)

        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self):
        while self._node is not None:
            try:
                if not self._rclpy.ok():
                    break
                self._rclpy.spin_once(self._node, timeout_sec=0.005)
            except Exception:
                break        # context 关掉/退出时静默收尾，不刷栈

    def get(self):
        with self.lock:
            return self.rgb, self.depth

    def stop(self):
        try:
            self._node.destroy_node()
        except Exception:
            pass
        try:
            if self._rclpy.ok():
                self._rclpy.shutdown()
        except Exception:
            pass


def _msg_image_cls():
    from sensor_msgs.msg import Image
    return Image


def depth_at(depth, x, y):
    """取 (x,y) 处深度(mm)；无效/越界返回 -1（同 depth_worker 逻辑）。"""
    if depth is None:
        return -1
    h, w = depth.shape[:2]
    x = int(np.clip(x, 0, w - 1))
    y = int(np.clip(y, 0, h - 1))
    raw = depth[y, x]
    if depth.dtype == np.uint16:
        return int(raw) if raw > 0 else -1
    return int(raw) if raw > 0 else -1


def main():
    args = parse_args()
    os.makedirs(SAVE_DIR, exist_ok=True)

    # slice_ms 传 None 时用生产默认 _DEFAULT_SLICE_MS（当前 600），
    # 所以模式提示要看decoder 实际生效值，不能只看命令行参数
    decoder = QRDecoder(slice_ms=args.slice or None, fp_thresh=args.fp_thresh)
    if not decoder.available:
        print("pyzbar 不可用", file=sys.stderr)
        return 1
    mode = (f"渐进式搜索 时间片 {decoder.slice_ms}ms 指纹门 {decoder.fp_thresh}"
            if decoder.slice_ms > 0 else "旧行为（烧满预算，画面会冻）")
    print(f"QRDecoder v{__version__}  深度相机扫码  {mode}  "
          f"每 {args.interval} 帧解一次")

    detector = None
    if args.model:
        from vision.detector import Detector
        detector = Detector(args.model)
        detector.warmup()
        print(f"YOLO 已加载: {args.model}")

    cam = RosCamera(want_depth=not args.no_depth)
    cam.start()
    print(f"已订阅 {RGB_TOPIC}" + ("" if args.no_depth
                                   else f" + {DEPTH_TOPIC}"))
    print("等待相机出图…（若一直无画面，检查 hp60c.launch.py 是否已启动）")

    win = "depth_scan_test"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)

    total = ok = 0
    frame_seen = 0       # 收到的帧数（间隔取模用，与解码次数分开计）
    times = []
    max_gap = 0.0
    last_seq = 0
    depth_hits = []
    content_hits = Counter()
    paused = False
    prev_t = time.perf_counter()
    t_start = time.perf_counter()   # 运行起点（顶部显示运行时长）
    t_wait = time.perf_counter()
    warned_wait = False
    first_frame = True
    last_ms = 0.0                  # 最近一次解码耗时
    fps = 0.0

    while True:
        rgb, depth = cam.get()
        now = time.perf_counter()
        max_gap = max(max_gap, now - prev_t)
        fps = 0.9 * fps + 0.1 * (1.0 / max(1e-6, now - prev_t))
        prev_t = now

        if rgb is None:
            if now - t_wait > 3 and not warned_wait:
                print("…仍在等待画面")
                warned_wait = True
            if cv2.waitKey(30) & 0xFF in (ord("q"), 27):
                break
            continue
        if first_frame:
            h, w = rgb.shape[:2]
            print(f"相机出图: {w}x{h}  （条码要够宽：模块宽 ≥6px 才扛得住模糊）")
            first_frame = False

        results = []
        dets = []
        with cam.lock:
            fresh = cam.seq != last_seq
            last_seq = cam.seq

        # 只在"有新帧"时解码+重绘：HP60C 出帧率远低于本循环转速，
        # 无脑重绘会每秒上千次 copy+imshow 白烧 CPU，抢走解码和 GUI 的资源
        # （表现为窗口卡顿、鼠标不跟手）。
        if not fresh:
            # 空闲时也让按键有效（waitKey(10) 同时把空转降到 ~100Hz）
            key = cv2.waitKey(10) & 0xFF
            if key in (ord("q"), 27):
                break
            if key == ord(" "):
                paused = not paused
            continue

        show = rgb.copy()
        if not paused:
            # 注意：间隔必须按"收到的帧数"取模，不能按解码次数 total 取模
            # ——total 只在解码时 +1，拿它取模会导致 interval>1 时只解第一次
            frame_seen += 1
            if frame_seen % args.interval == 0:
                t0 = time.perf_counter()
                rois = None
                if detector is not None:
                    dets = detector.detect(show, conf=args.conf)
                    rois = [d["bbox"] for d in dets]
                results = decoder.decode(rgb, rois=rois) if rois is not None \
                    else decoder.decode(rgb)
                dt = (time.perf_counter() - t0) * 1000
                times.append(dt)
                last_ms = dt
                total += 1
                if results:
                    ok += 1
                    for r in results:
                        content_hits[r["content"]] += 1
                        d = depth_at(depth,
                                     (r["bbox"][0] + r["bbox"][2]) / 2,
                                     (r["bbox"][1] + r["bbox"][3]) / 2)
                        if d > 0:
                            depth_hits.append(d)

        for r in results:
            x1, y1, x2, y2 = [int(v) for v in r["bbox"]]
            cv2.rectangle(show, (x1, y1), (x2, y2), (0, 255, 0), 2)
            d = depth_at(depth, (x1 + x2) / 2, (y1 + y2) / 2)
            label = f"{r['content'][:24]} [{r.get('type', '')}]"
            if d > 0:
                label += f"  {d}mm"
            cv2.putText(show, label, (x1, max(18, y1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        for d in dets:
            x1, y1, x2, y2 = [int(v) for v in d["bbox"]]
            cv2.rectangle(show, (x1, y1), (x2, y2), (255, 0, 255), 1)

        head_lines = [
            f"v{__version__}  slice={decoder.slice_ms}ms "
            f"gate={decoder.fp_thresh}   run {int(now - t_start)}s",
            f"decode {last_ms:6.1f} ms   fps {fps:4.1f}   "
            f"ok {ok}/{total} ({100.0 * ok / max(1, total):.0f}%)",
            "[space] pause  [s] save  [q] quit",
        ]
        if decoder.slice_ms <= 0:
            head_lines[0] += "  [OLD: burns full budget]"
        for i, line in enumerate(head_lines):
            cv2.putText(show, line, (8, 24 + i * 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 200, 255), 2)
        if depth_hits:
            cv2.putText(show, f"depth {depth_hits[-1]} mm", (8, 24 + 3 * 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 160), 2)
        if paused:
            cv2.putText(show, "PAUSED", (8, 24 + 4 * 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)
        cv2.imshow(win, show)

        key = cv2.waitKey(1) & 0xFF
        if key in (ord("q"), 27):
            break
        if key == ord(" "):
            paused = not paused
        elif key == ord("s") and rgb is not None:
            path = os.path.join(SAVE_DIR,
                                "depth_" + datetime.now().strftime(
                                    "%H%M%S_%f") + ".png")
            cv2.imwrite(path, rgb)
            print(f"已保存 {path}")

    cam.stop()
    cv2.destroyAllWindows()

    print("\n===== 深度相机扫码统计 =====")
    print(f"解码 {total} 次  成功 {ok}  "
          f"成功率 {100.0 * ok / max(1, total):.1f}%")
    if times:
        print(f"耗时: 平均 {sum(times) / len(times):.1f} ms  "
              f"最坏 {max(times):.1f} ms")
    if max_gap:
        print(f"预览最大间隔: {max_gap * 1000:.0f} ms")
    if depth_hits:
        print(f"深度读数(mm): 平均 {sum(depth_hits) / len(depth_hits):.0f}  "
              f"最近 {depth_hits[-1]}  样本 {len(depth_hits)}")
    if content_hits:
        print("内容命中:")
        for c, n in content_hits.most_common():
            print(f"  {n:>4} x  {c[:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())