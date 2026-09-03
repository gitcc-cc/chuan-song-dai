// ota.h
// OTA（Over The Air）无线烧录模块
// 功能：ESP32连上WiFi后，电脑通过局域网就能远程烧录代码，不用插USB线
// 原理：ESP32在WiFi上开一个OTA服务，电脑通过这个服务传输新固件
#ifndef MY_OTA_H
#define MY_OTA_H

#include <Arduino.h>
#include <WiFi.h>
#include <ArduinoOTA.h>

// 初始化OTA服务
// 在setup()里，tcpConnectWiFi()之后调用
void otaBegin() {
    // 设置OTA的主机名，电脑上看到的设备名
    ArduinoOTA.setHostname("ESP32-S3");

    // 可选：设置OTA密码，防止别人乱烧你的板子
    // 不想要密码就注释掉
     ArduinoOTA.setPassword("1234");

    // OTA开始时的回调，打印正在更新什么
    ArduinoOTA.onStart([]() {
        String type;
        if (ArduinoOTA.getCommand() == U_FLASH) {
            type = "程序";         // 更新程序代码
        } else {
            type = "文件系统";     // 更新SPIFFS文件
        }
        Serial.printf("[OTA] 开始更新: %s\n", type.c_str());
    });

    // OTA完成时的回调
    ArduinoOTA.onEnd([]() {
        Serial.println("\n[OTA] 更新完成！即将重启...");
    });

    // OTA传输进度的回调，显示百分比
    ArduinoOTA.onProgress([](unsigned int progress, unsigned int total) {
        Serial.printf("[OTA] 进度: %u%%\r", (progress / (total / 100)));
    });

    // OTA出错时的回调，打印错误类型
    ArduinoOTA.onError([](ota_error_t error) {
        Serial.printf("[OTA] 错误[%u]: ", error);
        if (error == OTA_AUTH_ERROR) Serial.println("认证失败");
        else if (error == OTA_BEGIN_ERROR) Serial.println("启动失败");
        else if (error == OTA_CONNECT_ERROR) Serial.println("连接失败");
        else if (error == OTA_RECEIVE_ERROR) Serial.println("接收失败");
        else if (error == OTA_END_ERROR) Serial.println("结束失败");
    });

    // 启动OTA服务，开始监听烧录请求
    ArduinoOTA.begin();

    // 打印IP地址，OTA烧录时需要填这个地址
    Serial.printf("[OTA] 就绪！IP: %s\n", WiFi.localIP().toString().c_str());
    Serial.println("[OTA] 等待无线烧录...");
}

// OTA处理函数，必须在loop()里每轮都调用
// 不调用就收不到电脑发来的烧录请求
void otaLoop() {
    ArduinoOTA.handle();
}

#endif