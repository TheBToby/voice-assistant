# 声网 ESP32 大模型智能对话

*简体中文| [English](README.md)*

## 例程简介

本例程演示了如何通过Seeed studio XIAO ESP32S3，模拟一个典型的大模型智能对话场景，可以演示进入大模型进行实时智能对话。

### 文件结构
```
├── CMakeLists.txt
├── components                                  Agora iot sdk component
│   ├── agora_iot_sdk
│   │   ├── CMakeLists.txt
│   │   ├── include                             Agora iot sdk header files
│   │   │   ├── agora_rtc_api.h
│   │   └── libs                                Agora iot sdk libraries
│   │       ├── libagora-cjson.a
│   │       ├── libahpl.a
│   │       ├── librtsa.a
|   ├── esp32-camera                           esp32-camera component submodule
├── main                                        LLM Demo code
│   ├── ai_agent.h
│   ├── app_config.h
│   ├── common.h
│   ├── audio_proc.h
│   ├── rtc_proc.h
│   ├── CMakeLists.txt
│   ├── Kconfig.projbuild
|   ├── ai_agent.c
|   ├── audio_proc.c
|   ├── rtc_proc.c
│   └── llm_main.c
├── partitions.csv                              partition table
├── README.en.md
├── README.md
├── sdkconfig.defaults
└── sdkconfig.defaults.esp32s3
```
---

## 目录文件一览

- `llm_main.c`
  程序入口 `app_main()`：WiFi → AIC3104 初始化 → `ai_agent_generate()` → 创建 RTC → 提示按键启动 Agent → 定时 ping + 打印内存。

- `app_config.h`
  业务配置（TEN AI Agent 服务地址、Graph、Channel、UID、AppID 等）。

- `common.h`
  全局状态 `g_app`、音频编解码宏选择（G711U/G722）以及采样率/帧长等参数。

- `ai_agent.c / ai_agent.h`
  HTTP 客户端：请求 TEN AI Agent 服务（`token/generate`、`start`、`stop`、`ping`），解析返回并写入 `g_app.app_id / g_app.token`。

- `rtc_proc.c / rtc_proc.h`
  Agora RTC 封装：join channel、事件回调、发送音频帧 `send_rtc_audio_frame()`、接收混音音频并播放 `playback_stream_write()`。

- `audio_proc.c / audio_proc.h`
  ESP-ADF 音频 pipeline：I2S 录音 → AEC（`algo_stream`）→ raw → 发送到 RTC；下行音频写入 raw → I2S 播放。
  备注：`audio_set_volume()` 目前是占位打印（未实现 AIC3104 音量寄存器控制）。

- `aic3104_ng.c / aic3104_ng.h`
  AIC3104 Codec I2C 最小驱动（探测/写寄存器/默认初始化）。

- `xvf3800.c / xvf3800.h`
  XVF3800（XMOS）I2C 控制与按键轮询任务：
  - SET → `ai_agent_start()`
  - MUTE → `ai_agent_stop()`

- `video_proc.c / video_proc.h`（已保留但默认不启用）
  CMakeLists 里已注释 `video_proc.c`，且 `llm_main.c` 中 `start_video_proc()` 被注释。

---
## 环境配置

### 硬件要求

本例程由`ESP32-S3-Korvo-2 V3`开发板移植而来，支持Seeed Studio `Respeaker XVF3800`

## 编译和下载

### esp32-camera

编译运行本示例需要 esp32-camera 组件。该组件已作为子模块添加到 `components/esp32-camera` 目录中。请运行以下命令克隆子模块：

```bash
git submodule update --init --recursive
```

### Agora IOT SDK

编译运行本示例需要 Agora IoT SDK。Agora IoT SDK 可以在 [这里](https://rte-store.s3.amazonaws.com/agora_iot_sdk.tar) 下载。
将 `agora_iot_sdk.tar` 放到 `esp32-client/components` 目录下，并运行如下命令：

```bash
cd esp32-client/components
tar -xvf agora_iot_sdk.tar
```

### Linux 操作系统

#### 默认 IDF 分支

本例程支持 IDF tag v[5.2.3] 及以后的，例程默认使用 IDF tag v[5.2.3] (commit id: c9763f62dd00c887a1a8fafe388db868a7e44069)。

选择 IDF 分支的方法，如下所示：

```bash
cd $IDF_PATH
git checkout v5.2.3
git pull
git submodule update --init --recursive
```

本例程支持 ADF v2.7 tag (commit id: 9cf556de500019bb79f3bb84c821fda37668c052)

#### 打上 IDF 补丁

本例程还需给 IDF 合入1个 patch， 合入命令如下：

```bash
export ADF_PATH=~/esp/esp-adf
cd $IDF_PATH
git apply $ADF_PATH/idf_patches/idf_v5.2_freertos.patch
```

#### 编译固件

将本例程(esp32-client)目录拷贝至 ~/esp 目录下。请运行如下命令：

```bash
$ . $HOME/esp/esp-idf/export.sh
$ cd ~/esp/esp32-client
$ idf.py set-target esp32s3
$ idf.py menuconfig	--> Agora Demo for ESP32 --> (配置 WIFI SSID 和 Password)
$ idf.py build
```
配置freertos的前向兼容能力
在menuconfig中Component config --> FreeRTOS --> Kernel设置configENABLE_BACKWARD_COMPATIBILITY

### Windows 操作系统

#### 默认 IDF 分支

下载IDF，选择v5.2.3 offline版本下载，例程默认使用 IDF tag v[5.2.3]
https://docs.espressif.com/projects/esp-idf/zh_CN/v5.2.3/esp32/get-started/windows-setup.html

下载ADF，ADF目录Espressif/frameworks，为支持ADF v2.7 tag (commit id: 9cf556de500019bb79f3bb84c821fda37668c052)
https://docs.espressif.com/projects/esp-adf/zh_CN/latest/get-started/index.html#step-2-get-esp-adf


#### 打上 IDF 补丁

方法一.系统设置中将ADF_PATH添加到环境变量
E:\esp32s3\Espressif\frameworks\esp-adf
方法二.命令行中将ADF_PATH添加到环境变量

```bash
$ setx ADF_PATH Espressif/frameworks/esp-adf
```

注意：ADF_PATH环境变量设置后，重启ESP-IDF 5.2 PowerShell生效

本例程还需给 IDF 合入1个 patch， 合入命令如下：

```bash
cd $IDF_PATH
git apply $ADF_PATH/idf_patches/idf_v5.2_freertos.patch
```

---
#### 修改 ESP-ADF Board 引脚配置

这是**最关键**的一步！需要修改 ESP-ADF 框架中的 Board 层配置。

**文件位置（示例）**
根据自己的ESPADF目录为准

**Windows**：
```
D:\Espressif\frameworks\esp-adf\components\audio_board\esp32_s3_korvo2_v3\board_pins_config.c
```

**Linux/Mac**：
```
~/esp/esp-adf/components/audio_board/esp32_s3_korvo2_v3/board_pins_config.c
```

或者根据你的 `$ADF_PATH` 环境变量：
```
$ADF_PATH/components/audio_board/esp32_s3_korvo2_v3/board_pins_config.c
```
修改 I2C 引脚配置

找到 `get_i2c_pins()` 函数，修改为：

```c
esp_err_t get_i2c_pins(i2c_port_t port, i2c_config_t *i2c_config)
{
    // 注释掉原来的 Korvo-2 配置
    // AUDIO_NULL_CHECK(TAG, i2c_config, return ESP_FAIL);
    // if (port == I2C_NUM_0 || port == I2C_NUM_1) {
    //     i2c_config->sda_io_num = GPIO_NUM_17;
    //     i2c_config->scl_io_num = GPIO_NUM_18;
    // } else {
    //     i2c_config->sda_io_num = -1;
    //     i2c_config->scl_io_num = -1;
    //     ESP_LOGE(TAG, "i2c port %d is not supported", port);
    //     return ESP_FAIL;
    // }

    // ReSpeaker XVF3800 I2C 配置
    i2c_config->sda_io_num = GPIO_NUM_5;   // ReSpeaker I2C SDA
    i2c_config->scl_io_num = GPIO_NUM_6;   // ReSpeaker I2C SCL
    return ESP_OK;
}
```

#### 修改 I2S 引脚配置

找到 `get_i2s_pins()` 函数，修改为：

```c
esp_err_t get_i2s_pins(int port, board_i2s_pin_t *i2s_config)
{
    // 注释掉原来的 Korvo-2 配置
    // AUDIO_NULL_CHECK(TAG, i2s_config, return ESP_FAIL);
    // if (port == 0) {
    //     i2s_config->bck_io_num = GPIO_NUM_9;
    //     i2s_config->ws_io_num = GPIO_NUM_45;
    //     i2s_config->data_out_num = GPIO_NUM_8;
    //     i2s_config->data_in_num = GPIO_NUM_10;
    //     i2s_config->mck_io_num = GPIO_NUM_16;
    // } else if (port == 1) {
    //     i2s_config->bck_io_num = -1;
    //     i2s_config->ws_io_num = -1;
    //     i2s_config->data_out_num = -1;
    //     i2s_config->data_in_num = -1;
    //     i2s_config->mck_io_num = -1;
    // } else {
    //     memset(i2s_config, -1, sizeof(board_i2s_pin_t));
    //     ESP_LOGE(TAG, "i2s port %d is not supported", port);
    //     return ESP_FAIL;
    // }

    // ReSpeaker XVF3800 I2S 配置
    i2s_config->bck_io_num   = GPIO_NUM_8;   // BCLK
    i2s_config->ws_io_num    = GPIO_NUM_7;   // WS/LRCK
    i2s_config->data_out_num = GPIO_NUM_44;  // DOUT
    i2s_config->data_in_num  = GPIO_NUM_43;  // DIN
    i2s_config->mck_io_num   = -1;           // 禁用 MCLK（先保证稳定）
    return ESP_OK;
}
```

**⚠️ 重要说明**：
- 这个文件在 ESP-ADF 框架目录中，不在项目目录
- 修改后会影响所有使用这个 Board 配置的项目
- 建议备份原文件：`cp board_pins_config.c board_pins_config.c.backup`

---
### 配置你自己的 AI Agent

1. 请在 `app_config.h` 文件中配置你自己的 AI Agent。
2. 修改 `TENAI_AGENT_URL` 为你自己的 TEN-Agent 服务器 URL (一般为你通过 `task run` 启动的8080服务)。
3. 修改 `AI_AGENT_CHANNEL_NAME` 为你自己的 AI Agent Channel 名称。
4. 配置 WIFI SSID 和 Password
6. 重新编译后烧录到芯片上。

注意：

1. 请确认开发板上已至少接入一个扬声器。
2. 服务端已经运行agora task。
---
### 编译固件

将本例程(esp32-client)目录拷贝至 Espressif/frameworks 目录下。请运行如下命令：
```bash
$ cd ../esp32-client
$ idf.py set-target esp32s3
$ idf.py menuconfig	--> Agora Demo for ESP32 --> (配置 WIFI SSID 和 Password)
$ idf.py build
```
配置freertos的前向兼容能力
在menuconfig中Component config --> FreeRTOS --> Kernel设置configENABLE_BACKWARD_COMPATIBILITY

---
### 下载固件

请运行如下命令：

```bash
$ idf.py -p /dev/ttyUSB0 flash monitor
```
注意：Linux系统中可能会遇到 /dev/ttyUSB0 权限问题，请执行 sudo usermod -aG dialout $USER

下载成功后，本例程会自动运行，待设备端加入RTC频道完成后，可以看到串口打印："Agora: Press [SET] key to Join the Ai Agent ..."。


## 常见问题

### Q1: 编译时出现 `i2c driver install error`

**原因**：I2C 驱动冲突，新旧 API 同时使用

**解决方案**：
- 确保 `aic3104_ng.c` 使用的是 `driver/i2c.h`（旧 API）
- 确保没有使用 `driver/i2c_master.h`（新 API）
- 检查代码中是否调用了 `i2c_driver_delete()` 删除旧驱动

### Q2: 运行时 I2C 超时 `ESP_ERR_TIMEOUT`

**可能原因**：
1. 硬件连接问题
2. 引脚配置错误
3. I2C 地址错误
4. 没有上拉电阻

**调试步骤**：
1. 检查日志中的 I2C 扫描结果：
   ```
   W (xxxx) AIC3104_NG: Scanning I2C bus...
   W (xxxx) AIC3104_NG: Found device at address 0x??
   ```
2. 如果没有检测到设备，检查硬件连接
3. 如果检测到设备但地址不是 0x18，修改 `aic3104_ng.h` 中的 `AIC3104_ADDR`

### Q3: 编译错误 `audio_board_init` 未定义

**原因**：`audio_proc.c` 中没有正确注释掉相关代码

**解决方案**：
确保 `setup_audio()` 函数中所有调用 `board_handle` 的代码都已注释

### Q4: 音频没有声音

**可能原因**：
1. AIC3104 初始化失败
2. I2S 引脚配置错误
3. 音频数据路径问题

**调试步骤**：
1. 检查日志确认 AIC3104 初始化成功：
   ```
   AIC3104 detected, page register = 0x00
   ~~~~~AIC3104 Codec initialized successfully~~~~
   ```
2. 确认 `board_pins_config.c` 中的 I2S 引脚配置正确
3. 使用示波器检查 I2S 信号线是否有波形

### Q5: 网络缓冲区错误 `Not enough space`

**现象**：
```
[7479.598][err] -1/12:Not enough space
[7480.643][err][][netc_send_udp_data:185] 28 calls suppressed
```

**原因**：这是运行时的网络问题，不影响硬件初始化

**解决方案**：
1. /（`idf.py menuconfig`）
2. 检查网络质量
3. 降低音频码率

### Q6: 修改 `board_pins_config.c` 后仍然报错

**原因**：修改的文件路径不对或没有重新编译 ESP-ADF

**解决方案**：
1. 确认修改的是正确的文件（使用 `echo $ADF_PATH` 或 `echo %ADF_PATH%` 查看路径）
2. 运行 `idf.py fullclean` 完全清理
3. 重新编译 `idf.py build`

---

## 验证结果

### 成功的启动日志

如果一切配置正确，应该看到类似以下日志：

```
I (xxxx) wifi:connected with YourWiFi, aid = 1, channel 6, BW20, bssid = xx:xx:xx:xx:xx:xx
got ip: 10.103.4.61

~~~~~Initializing AIC3104 Codec~~~~
E (2342) i2c: i2c_driver_delete(457): i2c driver install error
W (2349) AIC3104_NG: init done: port=0 SDA=5 SCL=6 speed=100000
W (2355) AIC3104_NG: Scanning I2C bus...
W (2360) AIC3104_NG: Found device at address 0x18
W (2365) AIC3104_NG: Found 1 I2C device(s)
W (2370) AIC3104_NG: probe: write page 0
W (2375) AIC3104_NG: probe ok: page reg=0x00
AIC3104 detected, page register = 0x00
W (2380) AIC3104_NG: default setup applied
~~~~~AIC3104 Codec initialized successfully~~~~

I (xxxx) AUDIO_PIPELINE: Pipeline started
~~~~~agora_rtc_join_channel success~~~~
Agora: Press [SET] key to join the Ai Agent ...
```

### 关键成功标志

- ✅ `WiFi connected` - WiFi 连接成功
- ✅ `got ip: xxx.xxx.xxx.xxx` - 获取到 IP 地址
- ✅ `Found device at address 0x18` - 检测到 AIC3104 芯片
- ✅ `AIC3104 detected, page register = 0x00` - AIC3104 探测成功
- ✅ `AIC3104 Codec initialized successfully` - Codec 初始化成功
- ✅ `agora_rtc_join_channel success` - RTC 加入频道成功

---

## 总结

### 适配的核心要点

1. **Codec 驱动替换**：从 ES8311/ES7210 替换为 AIC3104
2. **I2C API 兼容**：使用旧 I2C API 避免驱动冲突
3. **引脚重新映射**：修改 Board 层配置适配新硬件
4. **跳过 Board 初始化**：直接初始化 AIC3104，不依赖 ESP-ADF Board 层

### 修改的文件统计

- **新增文件**: 2 个
- **修改项目文件**: 3 个
- **修改框架文件**: 1 个（ESP-ADF）

### 已知限制

1. **音量控制**：暂时禁用，需要通过 AIC3104 寄存器实现
2. **MCLK**：禁用状态，如果需要可以启用并配置引脚
3. **网络缓冲区**：运行时可能出现缓冲区不足警告

### 参考资源

- [ESP-IDF 编程指南](https://docs.espressif.com/projects/esp-idf/zh_CN/v5.2.3/esp32s3/)
- [ESP-ADF 编程指南](https://docs.espressif.com/projects/esp-adf/zh_CN/latest/)
- [TI AIC3104 数据手册](https://www.ti.com/product/TLV320AIC3104)
- [ReSpeaker 参考项目](https://github.com/zhannn668/esp32-client-respeaker)

---

## 许可证

本适配指南基于 TEN Framework 的 MIT 许可证。

---

**文档版本**: 1.0
**创建日期**: 2025-01-05
**适用硬件**: ReSpeaker XVF3800 + ESP32-S3
**适用软件**: TEN Framework esp32-client + ESP-IDF 5.2.3 + ESP-ADF 2.7
## 关于声网

声网音视频物联网平台方案，依托声网自建的底层实时传输网络 Agora SD-RTN™ (Software Defined Real-time Network)，为所有支持网络功能的 Linux/RTOS 设备提供音视频码流在互联网实时传输的能力。该方案充分利用了声网全球全网节点和智能动态路由算法，与此同时支持了前向纠错、智能重传、带宽预测、码流平滑等多种组合抗弱网的策略，可以在设备所处的各种不确定网络环境下，仍然交付高连通、高实时和高稳定的最佳音视频网络体验。此外，该方案具有极小的包体积和内存占用，适合运行在任何资源受限的 IoT 设备上，包括乐鑫 ESP32 全系列产品。

## 技术支持

请按照下面的链接获取技术支持：

- 如果发现了示例代码的 bug 和有其他疑问，可以直接联系社区负责人

我们会尽快回复。
