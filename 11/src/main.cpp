// main.cpp
// ESP32 控制程序：按钮4状态 + 电位器调速 + TCP远程控制 + OTA无线烧录
// 速度模式：就地（电位器） / 远程（上位机滑块），默认就地
// 按钮循环：停止→正转→反转→自动→停止
// 自动模式：光纤传感器LOW=有物体→停，HIGH=无物体→转
#include <Arduino.h>
#include "myMqtt.h"
#include "ota.h"
#include <SCSCL.h>
#include <Adafruit_NeoPixel.h>

SCSCL sc;

// ============ 引脚 ============
#define BTN_PIN     4     // 按钮（内部上拉，按下=LOW）
#define ADC_PIN     7     // 电位器
#define MOTOR_A     18    // 传送带电机A
#define MOTOR_B     15    // 传送带电机B
#define SENSOR_PIN  5     // 光纤传感器（NPN：LOW=检测到物体，HIGH=无物体）
#define DJ_RX       16    // 舵机串口RX
#define DJ_TX       17    // 舵机串口TX

// ============ 舵机 ============
#define SERVO_ID    6     // 舵机ID
// 速度寄存器(地址46)：0=停，1~13=反转慢→快，128~141=正转慢→快

// ============ RGB灯 ============
#define NEOPIXEL_PIN  48    // ESP32-S3 板载RGB灯
#define NUM_PIXELS    1
Adafruit_NeoPixel strip(NUM_PIXELS, NEOPIXEL_PIN, NEO_GRB + NEO_KHZ800);

// ============ 状态变量 ============
int state = 0;             // 0=停止 1=正转 2=反转 3=自动
int lastPot = -1;          // 电位器上一次值(0~13)
int currentSpeed = 0;      // 当前实际速度(0~13)，status查询返回这个
int speedMode = 0;         // 0=就地模式（电位器） 1=远程模式（滑块）
bool lastButton = HIGH;    // 按钮上一次状态
unsigned long debounceTime = 0; // 按钮消抖时间戳
// ============ 时间 ============
bool sensorTriggered = false;
unsigned long sensorTriggerTime = 0;
// ============ 定时器 ============
void timerStart(bool *flag, unsigned long *timestamp) {
    if (!*flag) {
        *flag = true;
        *timestamp = millis();
    }
}
bool timerDone(bool *flag, unsigned long *timestamp, unsigned long ms) {
    return *flag && (millis() - *timestamp >= ms);
}
void timerReset(bool *flag) {
    *flag = false;
}

// ============ 网络 ============
bool wifiReady = false;    // WiFi是否已连接
bool netInitDone = false;  // TCP/OTA是否已初始化

// ============ 传送带电机控制 ============
// 电机控制：A=0,B=0停 | A=1,B=0正转 | A=1,B=1反转
void motorStop()    { digitalWrite(MOTOR_A, LOW);  digitalWrite(MOTOR_B, LOW);  }
void motorForward() { digitalWrite(MOTOR_A, HIGH); digitalWrite(MOTOR_B, LOW);  }
void motorReverse() { digitalWrite(MOTOR_A, HIGH); digitalWrite(MOTOR_B, HIGH); }



// ============ 更新所有设备 ============
// 根据 state 和 speed 统一控制舵机+传送带
void updateDevices(int speed) {
    currentSpeed = speed;  // 记录实际速度
    switch (state) {
        case 0:  // 停止
            sc.writeWord(SERVO_ID, 46, 0);
            motorStop();
            break;
        case 1:  // 正转：舵机正转(128+speed)，传送带正转
            sc.writeWord(SERVO_ID, 46, 128 + speed);
            motorForward();
            break;
        case 2:  // 反转：舵机反转(speed)，传送带反转
            sc.writeWord(SERVO_ID, 46, speed);
            motorReverse();
            break;
        case 3:  // 自动：传感器LOW=有物体→停，HIGH=无物体→正转
          
            break;
    }
}
// ============ 设置状态 ============
// state变了时调用，强制刷新设备
void setState(int newState) {
    state = newState;
    lastPot = -1;  // 强制下次刷新
    updateDevices(currentSpeed);
    switch (state) {
        case 0: Serial.println(">>> 停止");     strip.clear(); strip.show(); break;
        case 1: Serial.println(">>> 正转");     strip.clear(); strip.show(); break;
        case 2: Serial.println(">>> 反转");     strip.clear(); strip.show(); break;
        case 3: Serial.println(">>> 自动模式"); break;
    }
}
// ============ TCP命令处理 ============
void handleCommand() {
    String cmd = mqttReceive();
    if (cmd.length() == 0) return;

    // -- 查询状态：返回JSON，上位机用来更新界面 --
    if (cmd == "status") {
        int potPercent = (lastPot < 0) ? 0 : map(lastPot, 0, 13, 0, 100);
        String json = "{\"state\":" + String(state) +
                      ",\"speed\":" + String(currentSpeed) +
                      ",\"pot\":" + String(potPercent) +
                      ",\"sensor\":" + String(digitalRead(SENSOR_PIN)) +
                      ",\"mode\":" + String(speedMode) +
                      "}";
        mqttSend(json);
    }
    // -- 远程调速：只有远程模式才生效 --
    else if (cmd.startsWith("speed ")) {
        if (speedMode == 1) {
            int val = cmd.substring(6).toInt();
            val = constrain(val, 0, 13);
            updateDevices(val);
            mqttSend("速度: " + String(val));
        } else {
            mqttSend("就地模式，远程调速无效");
        }
    }
    // -- 切换模式 --
    else if (cmd == "mode local") {
        speedMode = 0;
        lastPot = -1;  // 强制读一次电位器
        mqttSend("就地模式");
    }
    else if (cmd == "mode remote") {
        speedMode = 1;
        mqttSend("远程模式");
    }
    // -- 状态切换 --
    else if (cmd == "dj 0") { setState(0); mqttSend("停止");    }
    else if (cmd == "dj 1") { setState(1); mqttSend("正转");    }
    else if (cmd == "dj 2") { setState(2); mqttSend("反转");    }
    else if (cmd == "dj 3") { setState(3); mqttSend("自动模式"); }
    // -- 测试通信 --
    else if (cmd == "hello") { mqttSend("你好！我是 ESP32"); }
    // -- 抱闸控制 --
    else if (cmd == "en 0") {
        sc.writeWord(SERVO_ID, 46, 0);
        delay(100);
        sc.writeByte(SERVO_ID, 33, 0);   // 切位置模式
        delay(100);
        sc.EnableTorque(SERVO_ID, 0);    // 解除抱闸
        mqttSend("抱闸解除");
    }
    else if (cmd == "en 1") {
        sc.EnableTorque(SERVO_ID, 1);    // 上抱闸
        delay(100);
        sc.writeByte(SERVO_ID, 33, 1);   // 切轮模式
        delay(100);
        mqttSend("抱闸锁定");
    }
    // -- 未知命令 --
    else { mqttSend("未知命令: " + cmd); }
}

// ============ 初始化 ============
   void setup() {
    Serial.begin(115200);
    delay(2000);    
    // 舵机串口
    Serial2.begin(1000000, SERIAL_8N1, DJ_RX, DJ_TX);
    sc.pSerial = &Serial2;
    delay(2000);

    // 引脚
    pinMode(BTN_PIN, INPUT_PULLUP);
    pinMode(SENSOR_PIN, INPUT);
    pinMode(MOTOR_A, OUTPUT);
    pinMode(MOTOR_B, OUTPUT);
    pinMode(LED_BUILTIN, OUTPUT);
    digitalWrite(LED_BUILTIN, LOW);

    // RGB灯初始化
    strip.begin();
    strip.setBrightness(50);
    strip.show();

    motorStop();

    // 舵机：上抱闸 → 切轮模式 → 停
    sc.EnableTorque(SERVO_ID, 1);
    delay(100);
    sc.writeByte(SERVO_ID, 33, 1);
    delay(100);
    sc.writeWord(SERVO_ID, 46, 0);

    Serial.println("===== 就绪 =====");
    mqttConnectWiFi();  // 发起WiFi连接（非阻塞）
}

// ============ 主循环 ============
void loop() {    
// --- WiFi重连状态机 
static int wifi_flag = 0;      // 0=就绪/断开, 1=正在重连
static unsigned long reconnect_ms = 0;

if (!mqttWiFiConnected()) { // 1. 检测到断开
        wifiReady = false;
        netInitDone = false;

    if (wifi_flag == 0) {     // 2. 标志为0
        reconnect_ms = millis(); // 记录时间戳
        wifi_flag = 1;        // 3. 修改标志为1，说明“正在连接”
       } }
if (wifi_flag == 1 && millis() - reconnect_ms > 5000) {
    Serial.println("[WiFi] 连接断开，尝试重新连接...");
    WiFi.begin(MY_WIFI_NAME, MY_WIFI_PASSWORD); // 触发重连
        wifi_flag = 2;
}
// ---- WiFi连接管理（非阻塞，只初始化一次） ----
    if (!netInitDone && mqttWiFiConnected()) {
        wifiReady = true;
        netInitDone = true;
        Serial.printf("[WiFi] 已连接！IP: %s\n", WiFi.localIP().toString().c_str());
        wifi_flag = 0;
        mqttConnectBroker();
        otaBegin();
    }
    if (wifiReady) otaLoop();

    // ---- 按钮（始终运行，消抖200ms） ----
    bool btn = digitalRead(BTN_PIN);
    if (btn == LOW && lastButton == HIGH && millis() - debounceTime > 200) {
        setState((state + 1) % 4);  // 0→1→2→3→0
        debounceTime = millis();
    }
    lastButton = btn;

    // ---- 电位器（每轮都读，就地模式才生效） ----
    int potNow = map(analogRead(ADC_PIN), 0, 4095, 0, 13);
    potNow = constrain(potNow, 0, 13);
    if (potNow != lastPot) {
        lastPot = potNow;
        if (speedMode == 0) {        // 就地模式：电位器控制
            updateDevices(potNow);
        }
        // 远程模式：只记录，不控制
    }
    // ---- 自动模式：每轮都检查传感器（独立于电位器） ----
    // if (state == 3) {
    //     if (digitalRead(SENSOR_PIN) == LOW) {
    //         // 检测到物体，停止
    //         sc.writeWord(SERVO_ID, 46, 0);
    //         motorStop();
    //     } else {
    //         // 没检测到，正转
    //         sc.writeWord(SERVO_ID, 46, 128 + potNow);
    //         motorForward();
    //     }
    // }
    // ---- 自动模式：每轮都检查传感器 ----
    if (state == 3) {
        if (digitalRead(SENSOR_PIN) == LOW) {
            // 检测到物体 → 蓝灯
            strip.setPixelColor(0, strip.Color(0, 0, 255));
            strip.show();
            timerStart(&sensorTriggered, &sensorTriggerTime);
            if (timerDone(&sensorTriggered, &sensorTriggerTime, 0)) {
                // 0.5秒到了，停
                sc.writeWord(SERVO_ID, 46, 0);
                motorStop();
            } else {
                // 0.5秒内继续转
                sc.writeWord(SERVO_ID, 46, 128 + potNow);
                motorForward();
            }
        } else {
            // 没检测到 → 白灯，立即正转
            strip.setPixelColor(0, strip.Color(255, 255, 255));
            strip.show();
            timerReset(&sensorTriggered);
            sc.writeWord(SERVO_ID, 46, 128 + potNow);
            motorForward();
        }
    }

    // ---- TCP命令（每轮都检查） ----
    if (wifiReady) {
            handleCommand();
    }
}