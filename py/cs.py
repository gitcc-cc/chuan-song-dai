import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image
from cv_bridge import CvBridge
import cv2
import numpy as np

class DepthTester(Node):
    def __init__(self):
        super().__init__('depth_tester')
        self.bridge = CvBridge()
        
        # 订阅彩色图
        self.sub_color = self.create_subscription(
            Image,
            '/ascamera_hp60c/camera_publisher/rgb0/image',
            self.color_callback,
            10)
        
        # 订阅深度图
        self.sub_depth = self.create_subscription(
            Image,
            '/ascamera_hp60c/camera_publisher/depth0/image_raw',
            self.depth_callback,
            10)
        
        print("等待相机数据...")
    
    def color_callback(self, msg):
        # 转成 OpenCV 格式
        color = self.bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')
        cv2.imshow("Color", color)
        cv2.waitKey(1)
    
    def depth_callback(self, msg):
        # 转成 numpy 数组（16位无符号整数，单位毫米）
        depth = self.bridge.imgmsg_to_cv2(msg, desired_encoding='16UC1')
        
        # 画面中心点的深度值
        h, w = depth.shape
        cx, cy = w // 2, h // 2
        center_depth = depth[cy, cx]
        
        print(f"画面中心 ({cx},{cy}) 距离: {center_depth} 毫米 = {center_depth/1000:.2f} 米")
        
        # 把深度图归一化后显示（方便肉眼观察）
        depth_vis = cv2.normalize(depth, None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U)
        depth_vis = cv2.applyColorMap(depth_vis, cv2.COLORMAP_JET)
        cv2.imshow("Depth", depth_vis)
        cv2.waitKey(1)

def main():
    rclpy.init()
    node = DepthTester()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        cv2.destroyAllWindows()
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()