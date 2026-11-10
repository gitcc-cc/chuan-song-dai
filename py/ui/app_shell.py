"""
应用主壳 (App Shell)
====================

【文件职责】
这是整个应用程序的主窗口框架文件，负责：
1. 创建主窗口 (继承自 QMainWindow)
2. 搭建"左侧菜单栏 + 右侧页面容器"的整体布局
3. 管理页面的注册、切换和生命周期
4. 保存/恢复窗口状态（窗口大小、侧边栏展开状态等）

【整体架构】
┌─────────────────────────────────────────────┐
│                  主窗口                       │
│  ┌─────────┬─────────────────────────────┐   │
│  │         │                             │   │
│  │ 侧边栏   │     页面容器 (StackedWidget) │   │
│  │ (菜单)   │                             │   │
│  │         │  ┌───────────────────────┐   │   │
│  │         │  │      当前显示的页面     │   │   │
│  │         │  └───────────────────────┘   │   │
│  └─────────┴─────────────────────────────┘   │
└─────────────────────────────────────────────┘

【核心组件】
- QMainWindow: PyQt6 主窗口基类，提供菜单栏、状态栏、工具栏等基础框架
- QStackedWidget: 页面堆栈容器，一次只显示一个页面（类似卡片切换）
- Sidebar: 自定义侧边栏组件，包含导航菜单按钮

【页面列表】
- 传送带控制页 (ConveyorPage) - 控制传送带电机
- 相机识别页 (CameraPage) - 单摄像头 YOLO 识别
- 产线控制页 (ProductionPage) - 多槽位生产线控制
"""
import os
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QStackedWidget, QApplication
)
from PyQt6.QtCore import Qt, QSettings
from PyQt6.QtGui import QIcon

from ui.sidebar import Sidebar
from ui.pages.conveyor_page import ConveyorPage
from ui.pages.camera_page import CameraPage
from ui.pages.production_page import ProductionPage
from core.config import get_config

# 图标路径常量
# _BASE_DIR: 项目根目录（py/ 的上级目录）
# _ICON_DIR: 图标资源目录，存放所有图标文件
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ICON_DIR = os.path.join(_BASE_DIR, "assets", "icons")


class AppShell(QMainWindow):
    """
    主窗口壳类：侧边栏 + 页面堆栈
    
    【类说明】
    这是应用程序的主窗口，采用经典的"侧边栏导航 + 内容区切换"布局模式。
    用户点击左侧菜单，右侧内容区切换到对应页面。
    
    【属性说明】
    - config: 配置管理器，用于保存/读取窗口状态
    - sidebar: 侧边栏导航组件
    - stack: 页面堆栈容器，存放所有页面
    - _pages: 字典，存储页面ID到页面对象的映射关系
    """

    def __init__(self):
        """
        初始化主窗口
        
        【执行顺序】
        1. 调用父类 QMainWindow 的构造函数
        2. 设置窗口标题和初始大小
        3. 设置窗口图标（任务栏/标题栏显示）
        4. 初始化配置管理器
        5. 创建中央部件和主布局（水平布局）
        6. 创建并添加侧边栏
        7. 创建页面堆栈容器
        8. 注册所有页面
        9. 恢复上次保存的窗口状态
        """
        # super().__init__() 调用父类构造函数，必须首先执行
        super().__init__()

        # 设置窗口标题，显示在标题栏
        self.setWindowTitle("控制系统")
        # 设置窗口初始大小：宽1200像素，高800像素
        # （实际显示时会按屏幕大小夹紧，见 _restore_state / _clamp_to_screen）
        self.resize(1200, 800)

        # 设置窗口图标（任务栏和标题栏显示的图标）
        # QIcon 用于加载图片作为图标
        icon_path = os.path.join(_ICON_DIR, "tubiao.png")
        if os.path.isfile(icon_path):  # 检查图标文件是否存在
            self.setWindowIcon(QIcon(icon_path))

        # 获取全局配置管理器单例，用于读取/保存窗口设置
        self.config = get_config()

        # 创建中央部件 (Central Widget)
        # QMainWindow 必须设置一个中央部件，所有内容都放在它里面
        central = QWidget()
        central.setStyleSheet("background-color: #f5f5f5;")  # 页面外底色，消除露白
        self.setCentralWidget(central)
        # QMainWindowLayout 会用 central 的 minimumSizeHint 强制窗口最小尺寸
        # （产线页 hint 高达 1226x847，小屏上窗口被撑出屏外且 resize 不生效）
        # 显式压到 1x1 解除限制，窗口大小完全由用户/代码控制
        central.setMinimumSize(1, 1)
        
        # 创建水平布局 (QHBoxLayout)，从左到右排列组件
        # 边距和间距都设为0，让侧边栏和页面紧贴在一起
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)  # 左、上、右、下边距
        main_layout.setSpacing(0)  # 组件之间的间距
        # 解除布局对窗口大小的强制：页面 minimumSizeHint（如产线页 1226x847）
        # 否则小屏（RDK HDMI 1024x573）上 show() 时窗口被撑到屏幕外，"缩不小"
        from PyQt6.QtWidgets import QLayout
        main_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)

        # 创建侧边栏导航组件
        self.sidebar = Sidebar()
        # 连接信号槽：当侧边栏点击菜单项时，触发页面切换
        # page_changed 信号传递页面ID字符串
        self.sidebar.page_changed.connect(self._switch_page)
        # 连接信号槽：当侧边栏展开/折叠时，保存状态
        # toggle_requested 信号传递展开状态布尔值
        self.sidebar.toggle_requested.connect(self._on_sidebar_toggle)
        # 将侧边栏添加到水平布局的左侧
        main_layout.addWidget(self.sidebar)

        # 创建页面堆栈容器 (QStackedWidget)
        # QStackedWidget 类似一叠卡片，一次只显示一张（一个页面）
        self.stack = QStackedWidget()
        # stretch=1 表示页面容器占据所有剩余空间（侧边栏宽度固定）
        main_layout.addWidget(self.stack, stretch=1)

        # 注册所有页面到堆栈
        self._pages = {}  # 字典：{'页面ID': 页面对象}
        self._register_pages()

        # 恢复上次保存的窗口状态（窗口大小、侧边栏状态等）
        self._restore_state()

    def _register_pages(self):
        """
        注册所有页面到堆栈容器和侧边栏
        
        【功能说明】
        按顺序创建三个功能页面，并将它们添加到：
        1. QStackedWidget（页面堆栈，用于显示切换）
        2. Sidebar（侧边栏菜单，用于导航点击）
        
        【页面创建流程】
        每个页面的创建过程：
        1. 实例化页面对象（如 ConveyorPage()）
        2. 准备图标路径
        3. 调用 _add_page() 完成注册
        
        【注意】
        页面顺序决定了侧边栏菜单的显示顺序和默认选中项
        """
        # ---- 传送带控制页 ----
        # 创建传送带控制页面实例
        conveyor = ConveyorPage()
        # 加载传送带页面的图标文件
        conveyor_icon = os.path.join(_ICON_DIR, "chuansongdai.png")
        # 注册页面：ID="conveyor"，图标为自定义图片
        self._add_page("conveyor", conveyor, "传送带控制", conveyor_icon)

        # ---- 相机识别页 ----
        # 创建相机识别页面实例
        camera = CameraPage()
        # 加载相机页面的图标文件
        camera_icon = os.path.join(_ICON_DIR, "xiaagnji.png")
        # 注册页面：ID="camera"，图标为自定义图片
        self._add_page("camera", camera, "相机识别", camera_icon)

        # ---- 产线控制页 ----
        # 创建产线控制页面实例
        production = ProductionPage()
        # 注册页面：ID="production"，图标使用 FontAwesome 图标名（非图片文件）
        self._add_page("production", production, "产线控制", "fa5s.industry")

        # 设置默认显示第一个页面（传送带控制）
        # setCurrentIndex(0) 显示索引为0的第一个页面
        self.stack.setCurrentIndex(0)
        # 同步更新侧边栏的选中状态，高亮显示"传送带控制"
        self.sidebar.set_current("conveyor")

    def _add_page(self, page_id: str, page_widget, title: str, icon_name: str):
        """
        添加单个页面到堆栈和侧边栏
        
        【参数说明】
        - page_id: str - 页面唯一标识符（如 "conveyor"），用于切换时查找页面
        - page_widget: QWidget - 页面控件对象（如 ConveyorPage 实例）
        - title: str - 页面标题，显示在侧边栏菜单中
        - icon_name: str - 图标名称或路径，显示在菜单文字旁边
        
        【执行步骤】
        1. 将页面对象存入字典 _pages，方便通过ID快速查找
        2. 将页面控件添加到 QStackedWidget（堆栈会自动管理页面）
        3. 将菜单项添加到侧边栏（显示图标+标题）
        """
        # 存入字典：{'页面ID': 页面对象}
        self._pages[page_id] = page_widget
        # 添加到堆栈容器（此时页面还未显示，只是加入"卡片堆"）
        self.stack.addWidget(page_widget)
        # 添加到侧边栏菜单（创建一个可点击的菜单项）
        self.sidebar.add_page(page_id, title, icon_name)

    def _switch_page(self, page_id: str):
        """
        切换到指定页面
        
        【功能说明】
        当用户点击侧边栏菜单项时，此方法被调用，负责：
        1. 隐藏当前正在显示的页面（调用 on_hide 方法）
        2. 在堆栈中切换显示目标页面
        3. 更新侧边栏的选中高亮状态
        4. 显示新页面（调用 on_show 方法）
        
        【参数说明】
        - page_id: str - 目标页面的唯一标识符（如 "conveyor"）
        
        【信号槽机制】
        此方法通过信号槽机制被调用：
        Sidebar.page_changed 信号 → _switch_page 槽函数
        
        【页面生命周期】
        页面切换时会触发页面的生命周期方法：
        - on_hide(): 页面即将隐藏，应停止摄像头/定时器等资源
        - on_show(): 页面即将显示，应恢复预览/启动资源
        """
        # 检查页面ID是否有效（存在于已注册页面中）
        if page_id in self._pages:
            # ---- 步骤1：隐藏当前页面 ----
            # 获取当前正在显示的页面对象
            current = self.stack.currentWidget()
            # 检查页面是否有 on_hide 方法（用 hasattr 安全检查）
            # hasattr(obj, "method_name") 检查对象是否有某属性/方法
            if hasattr(current, "on_hide"):
                # 调用页面的隐藏方法，通知它即将不可见
                current.on_hide()

            # ---- 步骤2：切换堆栈显示的页面 ----
            # setCurrentWidget() 会显示指定的页面，隐藏其他页面
            self.stack.setCurrentWidget(self._pages[page_id])
            # 同步更新侧边栏的选中状态（高亮显示当前菜单项）
            self.sidebar.set_current(page_id)

            # ---- 步骤3：显示新页面 ----
            # 获取新页面对象
            new_page = self._pages[page_id]
            # 检查新页面是否有 on_show 方法
            if hasattr(new_page, "on_show"):
                # 调用页面的显示方法，通知它即将可见
                new_page.on_show()

    def _on_sidebar_toggle(self, expanded: bool):
        """
        侧边栏展开/折叠状态变化时的回调
        
        【功能说明】
        当用户点击侧边栏的折叠/展开按钮时，保存当前状态，
        下次启动时可以恢复。
        
        【参数说明】
        - expanded: bool - True=展开状态，False=折叠状态
        
        【QSettings 说明】
        config 是 QSettings 的封装，提供简单的键值对存储。
        - set(key, value): 保存一个键值对
        - save(): 将内存中的配置写入磁盘（通常是 INI 文件或注册表）
        """
        # 保存侧边栏状态到配置文件
        self.config.set("sidebar_expanded", expanded)
        # 立即写入磁盘，确保程序异常退出时也能保存
        self.config.save()

    def _restore_state(self):
        """
        恢复上次保存的窗口状态
        
        【功能说明】
        程序启动时调用此方法，从配置文件中读取并恢复：
        1. 窗口位置和大小（几何信息）
        2. 侧边栏展开/折叠状态
        
        【窗口几何信息】
        窗口的几何信息（位置+大小）被编码为十六进制字符串存储。
        - saveGeometry() 返回 QByteArray（二进制数据）
        - toHex() 转换为十六进制字符串
        - restoreGeometry() 从十六进制字符串恢复窗口状态
        
        【首次启动】
        如果配置文件中没有保存过状态（首次启动），使用默认大小 1200x800
        """
        # ---- 恢复窗口几何（位置和大小） ----
        # 从配置中读取保存的窗口几何信息（十六进制字符串）
        geom_hex = self.config.get("window_geometry")
        # 当前屏幕可用区域（排除任务栏/面板）：窗口尺寸不能超过它，否则换小屏
        # （如 RDK 的 HDMI）时窗口会溢出到屏幕外，看起来"缩不小"
        screen = QApplication.primaryScreen().availableGeometry()
        if geom_hex:
            # 从十六进制字符串恢复窗口状态
            from PyQt6.QtCore import QByteArray
            # 将字符串编码为字节，再从十六进制解码为 QByteArray
            self.restoreGeometry(QByteArray.fromHex(geom_hex.encode()))
            self._clamp_to_screen(screen)
        else:
            # 首次启动，使用默认窗口大小（不超过屏幕的 90%）
            self.resize(min(1200, int(screen.width() * 0.9)),
                        min(800, int(screen.height() * 0.9)))

    def _clamp_to_screen(self, screen):
        """
        把窗口尺寸/位置夹紧到当前屏幕内

        场景：保存几何来自大屏（如笔记本 1920x1080），换到小屏（RDK HDMI
        1024x768）后 restoreGeometry 会把窗口放到屏幕外——标题栏拖不到、
        边框够不着，用户看起来就是"窗口缩不小"。这里统一夹紧。
        """
        g = self.geometry()
        new_w = min(g.width(), screen.width())
        new_h = min(g.height(), screen.height())
        if new_w != g.width() or new_h != g.height():
            self.resize(new_w, new_h)
        if not screen.contains(g.topLeft()):
            self.move(max(screen.left(), min(g.x(), screen.right() - new_w)),
                      max(screen.top(), min(g.y(), screen.bottom() - new_h)))

        # ---- 恢复侧边栏状态 ----
        # 读取侧边栏展开状态，默认为 True（展开）
        expanded = self.config.get("sidebar_expanded", True)
        # 设置侧边栏的展开/折叠状态
        self.sidebar.set_expanded(expanded)

    def closeEvent(self, event):
        """
        窗口关闭事件处理
        
        【功能说明】
        当用户关闭窗口时（点击X按钮或 Alt+F4），Qt 会自动调用此方法。
        我们重写此方法，在窗口关闭前执行清理工作。
        
        【清理顺序】
        1. 保存窗口几何状态（位置、大小）
        2. 通知当前页面保存自身状态
        3. 停止当前页面的资源（摄像头、定时器等）
        4. 清理所有页面的资源
        5. 调用父类的 closeEvent 完成真正的关闭
        
        【参数说明】
        - event: QCloseEvent - 关闭事件对象，包含关闭原因等信息
        
        【注意】
        如果不调用 super().closeEvent(event)，窗口将无法正常关闭！
        """
        # ---- 1. 保存窗口几何状态 ----
        # saveGeometry() 返回窗口位置和大小的 QByteArray
        # 转换为十六进制字符串存储到配置文件
        self.config.set("window_geometry", self.saveGeometry().toHex().data().decode())
        self.config.save()

        # ---- 2. 通知当前页面保存状态 ----
        # 获取当前显示的页面
        current = self.stack.currentWidget()
        # 如果页面有 save_state 方法（如保存用户输入的配置），调用它
        if hasattr(current, "save_state"):
            current.save_state()

        # ---- 3. 停止当前页面的资源 ----
        # 调用 on_hide 停止摄像头、定时器等
        if hasattr(current, "on_hide"):
            current.on_hide()

        # ---- 4. 清理所有页面的资源 ----
        # 遍历所有已注册的页面，调用 cleanup 方法释放资源
        # 产线页面可能有摄像头、YOLO模型等需要手动释放
        for page in self._pages.values():
            if hasattr(page, "cleanup"):
                page.cleanup()

        # ---- 5. 调用父类的关闭事件处理 ----
        # 这一步必须有！否则窗口关闭流程不会完成
        super().closeEvent(event)