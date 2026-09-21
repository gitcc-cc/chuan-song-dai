// myMqtt.h
// MQTT 通讯模块
// 只在启动时连接一次 Broker，失败后不再重连（想重连就重启 ESP32）
#ifndef MY_MQTT_H
#define MY_MQTT_H

#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>

// ==================== 配置 ====================
const char *MY_WIFI_NAME     = "14";
const char *MY_WIFI_PASSWORD = "111111111";
const char *MQTT_BROKER      = "10.156.130.56";
const int   MQTT_PORT        = 1883;
const char *MQTT_CLIENT_ID   = "esp32_conveyor";

// 主题
const char *TOPIC_CMD    = "ht/cmd/1";
const char *TOPIC_STATUS = "ht/status/1";

// ==================== 内部变量 ====================
WiFiClient _wifiClient;
PubSubClient _mqttClient(_wifiClient);
String _mqttReceivedMsg = "";

// ==================== MQTT 回调：收到消息时触发 ====================
void _mqttCallback(char* topic, byte* payload, unsigned int length) {
    String msg = "";
    for (unsigned int i = 0; i < length; i++) {
        msg += (char)payload[i];
    }
    msg.trim();
    _mqttReceivedMsg = msg;
    Serial.printf("[MQTT 收到] %s: %s\n", topic, msg.c_str());
}

// ==================== 发起 WiFi 连接（不阻塞）====================
void mqttConnectWiFi() {
    WiFi.mode(WIFI_STA);
    WiFi.begin(MY_WIFI_NAME, MY_WIFI_PASSWORD);
    Serial.printf("[WiFi] 正在连接: %s ...\n", MY_WIFI_NAME);
}

// ==================== 检查 WiFi 是否已连接 ====================
bool mqttWiFiConnected() {
    return (WiFi.status() == WL_CONNECTED);
}

// ==================== 连接 Broker（只调用一次）====================
bool mqttConnectBroker() {
    _mqttClient.setServer(MQTT_BROKER, MQTT_PORT);
    _mqttClient.setCallback(_mqttCallback);
    _mqttClient.setKeepAlive(60);
    _wifiClient.setTimeout(2000);  // 最多阻塞2秒

    Serial.printf("[MQTT] 连接 Broker: %s:%d ...\n", MQTT_BROKER, MQTT_PORT);

    if (_mqttClient.connect(MQTT_CLIENT_ID)) {
        Serial.println("[MQTT] 已连接 Broker");
        _mqttClient.subscribe(TOPIC_CMD);
        Serial.printf("[MQTT] 已订阅: %s\n", TOPIC_CMD);
        _mqttClient.publish(TOPIC_STATUS, "{\"online\":true}", true);
        return true;
    } else {
        Serial.printf("[MQTT] 连接失败，状态码: %d\n", _mqttClient.state());
        return false;
    }
}

// ==================== 发布状态消息 ====================
void mqttSend(String msg) {
    if (_mqttClient.connected()) {
        _mqttClient.publish(TOPIC_STATUS, msg.c_str());
        Serial.printf("[MQTT 发送] %s: %s\n", TOPIC_STATUS, msg.c_str());
    }
}

// ==================== 接收命令 ====================
// 已连接：正常处理消息
// 未连接：直接返回空字符串，不卡住，不重连
String mqttReceive() {
    if (_mqttClient.connected()) {
        _mqttClient.loop();
        String msg = _mqttReceivedMsg;
        _mqttReceivedMsg = "";
        return msg;
    }
    return "";  // 没连上就直接返回，什么都不做
}

// ==================== 检查连接状态 ====================
bool mqttIsConnected() {
    return _mqttClient.connected();
}

#endif // MY_MQTT_H