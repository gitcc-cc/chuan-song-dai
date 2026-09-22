"""
深度相机独立测试脚本
==================
验证 ROS2 订阅 + YOLO 检测 + 深度值读取。
独立运行，不依赖主程序。
用法: python depth_test.py
"""
import time
import numpy as np
import cv2

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
from sensor_msgs.msg import Image
from cv_bridge import CvBridge

from vision.detector import Detector


class DepthCameraTest(Node):
    """ROS2 节点：订阅彩色图 + 深度图，YOLO 检测，读中心深度值"""

    def __init__(self):
        super().__init__("depth_camera_test")
        self.bridge = CvBridge()
        self.rgb_frame = None
        self.depth_frame = None
        self.frame_count = 0

        qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
        )

        self.create_subscription(
            Image,
            "/ascamera_hp60c/camera_publisher/rgb0/image",
            self._rgb_callback,
            qos,
        )
        self.create_subscription(
            Image,
            "/ascamera_hp60c/camera_publisher/depth0/image_raw",
            self._depth_callback,
            qos,
        )

        # YOLO 检测器
        self.detector = None
        model_path = "/media/chow/CC/D+/yolo/EzYOLO-main/runs/train/exp_1/weights/best.pt"
        try:
            self.detector = Detector(model_path)
            self.detector.warmup()
            self.get_logger().info(f"YOLO 模型加载成功: {model_path}")
        except Exception as e:
            self.get_logger().warn(f"YOLO 模型加载失败: {e}，仅测试深度读取")

    def _rgb_callback(self, msg: Image):
        try:
            self.rgb_frame = self.bridge.imgmsg_to_cv2(msg, "bgr8")
        except Exception as e:
            self.get_logger().warn(f"RGB 转换失败: {e}")

    def _depth_callback(self, msg: Image):
        try:
            self.depth_frame = self.bridge.imgmsg_to_cv2(msg, "passthrough")
        except Exception as e:
            self.get_logger().warn(f"深度转换失败: {e}")

    def spin_once(self):
        rclpy.spin_once(self, timeout_sec=0.001)


def main():
    rclpy.init()
    node = DepthCameraTest()

    print("=" * 60)
    print("深度相机测试")
    print("  按 'q' 退出")
    print("  彩色图: /ascamera_hp60c/camera_publisher/rgb0/image")
    print("  深度图: /ascamera_hp60c/camera_publisher/depth0/image_raw")
    print("=" * 60)

    frame_count = 0
    last_time = time.time()

    try:
        while rclpy.ok():
            node.spin_once()

            if node.rgb_frame is None or node.depth_frame is None:
                cv2.waitKey(10)
                continue

            frame_count += 1
            rgb = node.rgb_frame.copy()
            depth = node.depth_frame.copy()

            # YOLO 检测
            detections = []
            if node.detector:
                detections = node.detector.infer(rgb, conf_threshold=0.5)

            # 画检测框 + 读深度
            for det in detections:
                bbox = det.get("bbox", [])
                if len(bbox) != 4:
                    continue
                x1, y1, x2, y2 = [int(v) for v in bbox]
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2

                # 框中心深度值
                if 0 <= cy < depth.shape[0] and 0 <= cx < depth.shape[1]:
                    raw = depth[cy, cx]
                    depth_mm = int(raw) if depth.dtype == np.uint16 else int(raw * 1000)
                    depth_text = f"{depth_mm}mm" if depth_mm > 0 else "N/A"
                else:
                    depth_text = "N/A"

                cls_name = det.get("cls_name", "?")
                conf = det.get("conf", 0)
                label = f"{cls_name} {conf:.2f} | {depth_text}"

                # 画框
                color = (0, 255, 0)
                cv2.rectangle(rgb, (x1, y1), (x2, y2), color, 2)

                # 标签背景
                (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 1)
                cv2.rectangle(rgb, (x1, y1 - th - 8), (x1 + tw + 4, y1), color, -1)
                cv2.putText(rgb, label, (x1 + 2, y1 - 4),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)

            # FPS
            now = time.time()
            fps = 1.0 / max(now - last_time, 1e-6)
            last_time = now
            cv2.putText(rgb, f"FPS: {fps:.1f}", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)

            # 显示
            cv2.imshow("Depth Camera Test - RGB", rgb)

            # 深度图伪彩色显示
            depth_vis = depth.copy()
            if depth.dtype == np.uint16:
                depth_vis = np.clip(depth_vis, 0, 1500)
                depth_vis = (depth_vis / 1500 * 255).astype(np.uint8)
            else:
                depth_vis = np.clip(depth_vis * 1000, 0, 1500)
                depth_vis = (depth_vis / 1500 * 255).astype(np.uint8)
            depth_color = cv2.applyColorMap(depth_vis, cv2.COLORMAP_JET)
            cv2.imshow("Depth Camera Test - Depth", depth_color)

            key = cv2.waitKey(1)
            if key == ord("q"):
                break

            # 限频打印
            if frame_count % 30 == 0:
                det_info = f", {len(detections)} detections" if detections else ""
                print(f"[帧 {frame_count}] FPS: {fps:.1f}{det_info}")

    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()
        cv2.destroyAllWindows()
        print(f"\n测试结束，共 {frame_count} 帧")


if __name__ == "__main__":
    main()
