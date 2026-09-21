// myTcp.h
// TCP通信模块（非阻塞版本）
// WiFi连接不会卡死程序，连不上也能正常运行本地功能
#ifndef MY_TCP_H
#define MY_TCP_H

#include <Arduino.h>
#include <WiFi.h>

// ==================== 配置 ====================
const char *MY_WIFI_NAME = "14";
const char *MY_WIFI_PASSWORD = "111111111";
const int MY_TCP_PORT = 8080;

// ==================== 内部变量 ====================
WiFiServer _tcpServer(MY_TCP_PORT);
WiFiClient _tcpClient;

// ==================== 发起WiFi连接（不等待）====================
// 只发一个连接请求就返回，不卡死
// 在setup()里调用一次就行
void tcpConnectWiFi() {
    WiFi.mode(WIFI_STA);
    WiFi.begin(MY_WIFI_NAME, MY_WIFI_PASSWORD);
    Serial.printf("[WiFi] 正在连接: %s ...\n", MY_WIFI_NAME);
    // 不等待，直接返回，loop()里会自动检查连上没有
}

// ==================== 检查WiFi是否已连接 ====================
// 返回true=已连接，返回false=还没连上
// 在loop()里每轮都调用
bool tcpWiFiConnected() {
    return (WiFi.status() == WL_CONNECTED);
}

// ==================== 启动TCP服务器 ====================
// WiFi连上后调用一次
void tcpBegin() {
    _tcpServer.begin();
    Serial.printf("[TCP] 监听端口: %d\n", MY_TCP_PORT);
}

// ==================== 发送消息 ====================
void tcpSend(String msg) {
    if (_tcpClient && _tcpClient.connected()) {
        _tcpClient.println(msg);
        Serial.printf("[发送] %s\n", msg.c_str());
    }
}

// ==================== 接收消息 ====================
// 有消息返回消息内容，没消息返回空字符串 ""
String tcpReceive() {
    // 检查新客户端
    WiFiClient newClient = _tcpServer.accept();
    if (newClient) {
        Serial.printf("[TCP] 新客户端: %s\n", newClient.remoteIP().toString().c_str());
        _tcpClient = newClient;
    }

    // 检查断开
    if (_tcpClient && !_tcpClient.connected()) {
        Serial.println("[TCP] 客户端断开");
        _tcpClient.stop();
        _tcpClient = WiFiClient();
    }

    // 读取消息
    if (_tcpClient && _tcpClient.connected() && _tcpClient.available()) {
        String msg = _tcpClient.readStringUntil('\n');
        msg.trim();
        if (msg.length() > 0) {
            Serial.printf("[收到] %s\n", msg.c_str());
        }
        return msg;
    }

    return "";
}

// ==================== 检查是否有客户端连接 ====================
bool tcpConnected() {
    return _tcpClient && _tcpClient.connected();
}

#endif