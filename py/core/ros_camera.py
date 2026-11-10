"""
深度相机 ROS2 节点生命周期（HP60C / ascamera）
============================================
软件级生命周期：main.py 启动时打开，退出时关闭，
界面上的「重启相机」按钮走 production_manager.restart_camera_node（不动）。

这里不 import rclpy（.venv 里没有 ROS），只管进程开关；
命令走 bash -c 'source ROS && ros2 launch'，与 run.sh/重启按钮同源。
"""
import subprocess
import time

_CAM_PKG = "ascamera"
_CAM_LAUNCH = "hp60c.launch.py"
_CAM_LOG = "/tmp/hp60c_camera.log"
# 环境链：系统 ROS + ascam 工作区 + SDK lib 路径
# 缺工作区/lib 路径 → libAngstrongCameraSdk.so 找不到（节点 exit 127）
_CAM_ENV = (
    f'source /opt/ros/humble/setup.bash && '
    f'source ~/ascam_ros2_ws/install/setup.bash && '
    f'export LD_LIBRARY_PATH=~/ascam_ros2_ws/src/ascamera/libs/lib/'
    f'aarch64-linux-gnu:~/ascam_ros2_ws/src/ascamera/libs/lib/x86_64-linux-gnu:$LD_LIBRARY_PATH && '
)


def ros_node_running() -> bool:
    """ascamera launch 进程是否在跑（pgrep 快速探测）。"""
    try:
        r = subprocess.run(["pgrep", "-f", _CAM_LAUNCH],
                           capture_output=True, check=False)
        return r.returncode == 0
    except Exception:
        return False


def start_ros_camera() -> bool:
    """
    启动深度相机节点（fire-and-forget，不阻塞）。

    已经在跑时不动它（避免重启 11 秒）。退场交给 stop_ros_camera。

    返回：
        True=这次调用启动了节点；False=本来就在跑
    """
    if ros_node_running():
        return False
    cmd = (
        f'{_CAM_ENV} '
        f'setsid ros2 launch {_CAM_PKG} {_CAM_LAUNCH} >> {_CAM_LOG} 2>&1 &'
    )
    subprocess.Popen(["/bin/bash", "-c", cmd], start_new_session=True)
    return True


def stop_ros_camera() -> None:
    """关闭深度相机节点（software 退出时用）。杀干净 launch/node 两层。"""
    for pattern in (_CAM_LAUNCH, "ascamera_node", "ros2 launch " + _CAM_PKG):
        try:
            subprocess.run(["pkill", "-f", pattern],
                           capture_output=True, check=False)
        except Exception:
            pass
    # 给节点一点收尾时间（设备释放看后面几秒，等太久没必要）
    time.sleep(0.5)
