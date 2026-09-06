# Agora ESP32 Large-Model Intelligent Conversation

*[简体中文](README.cn.md) | English*

## Example Overview

This example demonstrates how to use the Seeed Studio XIAO ESP32S3 to simulate a typical large-model intelligent conversation scenario, enabling real-time interactive dialogue with a large model.

### File Structure
```text
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

## Directory / Files at a Glance

- `llm_main.c`
  Program entry `app_main()`: WiFi → AIC3104 init → `ai_agent_generate()` → create RTC → prompt button to start Agent → periodic ping + print memory.

- `app_config.h`
  Business configuration (TEN AI Agent service URL, Graph, Channel, UID, AppID, etc.).

- `common.h`
  Global state `g_app`, audio codec macro selection (G711U/G722), and parameters such as sample rate/frame length.

- `ai_agent.c / ai_agent.h`
  HTTP client: requests TEN AI Agent service (`token/generate`, `start`, `stop`, `ping`), parses responses and writes into `g_app.app_id / g_app.token`.

- `rtc_proc.c / rtc_proc.h`
  Agora RTC wrapper: join channel, event callbacks, send audio frames `send_rtc_audio_frame()`, receive mixed audio and play via `playback_stream_write()`.

- `audio_proc.c / audio_proc.h`
  ESP-ADF audio pipeline: I2S recording → AEC (`algo_stream`) → raw → send to RTC; downlink audio writes to raw → I2S playback.
  Note: `audio_set_volume()` is currently a placeholder print (AIC3104 volume register control not implemented).

- `aic3104_ng.c / aic3104_ng.h`
  Minimal AIC3104 Codec I2C driver (probe / write registers / default initialization).

- `xvf3800.c / xvf3800.h`
  XVF3800 (XMOS) I2C control and key polling task:
  - SET → `ai_agent_start()`
  - MUTE → `ai_agent_stop()`

- `video_proc.c / video_proc.h` (kept but disabled by default)
  `video_proc.c` is commented out in CMakeLists, and `start_video_proc()` is commented out in `llm_main.c`.

---

## Environment Setup

### Hardware Requirements

This example is ported from the `ESP32-S3-Korvo-2 V3` development board and supports Seeed Studio `Respeaker XVF3800`.

## Build and Flash

### esp32-camera

To build and run this example, the esp32-camera component is required. It has been added as a submodule under `components/esp32-camera`. Run the following command to clone the submodule:

```bash
git submodule update --init --recursive
```

### Agora IOT SDK

To build and run this example, the Agora IoT SDK is required. You can download it from [here](https://rte-store.s3.amazonaws.com/agora_iot_sdk.tar).
Place `agora_iot_sdk.tar` into the `esp32-client/components` directory and run:

```bash
cd esp32-client/components
tar -xvf agora_iot_sdk.tar
```

### Linux

#### Default IDF Branch

This example supports IDF tag v[5.2.3] and later. By default it uses IDF tag v[5.2.3] (commit id: c9763f62dd00c887a1a8fafe388db868a7e44069).

To select the IDF branch:

```bash
cd $IDF_PATH
git checkout v5.2.3
git pull
git submodule update --init --recursive
```

This example supports ADF v2.7 tag (commit id: 9cf556de500019bb79f3bb84c821fda37668c052).

#### Apply the IDF Patch

This example also requires merging one patch into IDF. Run:

```bash
export ADF_PATH=~/esp/esp-adf
cd $IDF_PATH
git apply $ADF_PATH/idf_patches/idf_v5.2_freertos.patch
```

#### Build Firmware

Copy this example directory (esp32-client) to `~/esp`. Run:

```bash
$ . $HOME/esp/esp-idf/export.sh
$ cd ~/esp/esp32-client
$ idf.py set-target esp32s3
$ idf.py menuconfig  --> Agora Demo for ESP32 --> (configure WIFI SSID and Password)
$ idf.py build
```

Configure FreeRTOS forward compatibility:
In menuconfig: Component config --> FreeRTOS --> Kernel, enable `configENABLE_BACKWARD_COMPATIBILITY`.

### Windows

#### Default IDF Branch

Download IDF and choose the v5.2.3 offline version. This example uses IDF tag v[5.2.3] by default:
https://docs.espressif.com/projects/esp-idf/zh_CN/v5.2.3/esp32/get-started/windows-setup.html

Download ADF; the ADF directory is under Espressif/frameworks. To support ADF v2.7 tag (commit id: 9cf556de500019bb79f3bb84c821fda37668c052):
https://docs.espressif.com/projects/esp-adf/zh_CN/latest/get-started/index.html#step-2-get-esp-adf

#### Apply the IDF Patch

Method 1: Add `ADF_PATH` to system environment variables:
E:\esp32s3\Espressif\frameworks\esp-adf

Method 2: Add `ADF_PATH` in the command line:

```bash
$ setx ADF_PATH Espressif/frameworks/esp-adf
```

Note: After setting the ADF_PATH environment variable, restart ESP-IDF 5.2 PowerShell for it to take effect.

This example also requires merging one patch into IDF. Run:

```bash
cd $IDF_PATH
git apply $ADF_PATH/idf_patches/idf_v5.2_freertos.patch
```

---
#### Modify ESP-ADF Board Pin Configuration

This is the **most critical** step! You need to modify the Board-layer configuration in the ESP-ADF framework.

**File location (example)**
Depending on your own ESP-ADF directory:

**Windows**:
```text
D:\Espressif\frameworks\esp-adf\components\audio_board\esp32_s3_korvo2_v3\board_pins_config.c
```

**Linux/Mac**:
```text
~/esp/esp-adf/components/audio_board/esp32_s3_korvo2_v3/board_pins_config.c
```

Or based on your `$ADF_PATH` environment variable:
```text
$ADF_PATH/components/audio_board/esp32_s3_korvo2_v3/board_pins_config.c
```

Modify the I2C pin configuration

Find the `get_i2c_pins()` function and change it to:

```c
esp_err_t get_i2c_pins(i2c_port_t port, i2c_config_t *i2c_config)
{
    // Comment out the original Korvo-2 configuration
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

    // ReSpeaker XVF3800 I2C configuration
    i2c_config->sda_io_num = GPIO_NUM_5;   // ReSpeaker I2C SDA
    i2c_config->scl_io_num = GPIO_NUM_6;   // ReSpeaker I2C SCL
    return ESP_OK;
}
```

#### Modify the I2S Pin Configuration

Find the `get_i2s_pins()` function and change it to:

```c
esp_err_t get_i2s_pins(int port, board_i2s_pin_t *i2s_config)
{
    // Comment out the original Korvo-2 configuration
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

    // ReSpeaker XVF3800 I2S configuration
    i2s_config->bck_io_num   = GPIO_NUM_8;   // BCLK
    i2s_config->ws_io_num    = GPIO_NUM_7;   // WS/LRCK
    i2s_config->data_out_num = GPIO_NUM_44;  // DOUT
    i2s_config->data_in_num  = GPIO_NUM_43;  // DIN
    i2s_config->mck_io_num   = -1;           // Disable MCLK (ensure stability first)
    return ESP_OK;
}
```

**⚠️ Important Notes**:
- This file is in the ESP-ADF framework directory, not in the project directory.
- Changes will affect all projects using this Board configuration.
- It’s recommended to back up the original file: `cp board_pins_config.c board_pins_config.c.backup`

---
### Configure Your Own AI Agent

1. Configure your own AI Agent in the `app_config.h` file.
2. Change `TENAI_AGENT_URL` to your own TEN-Agent server URL (usually the 8080 service started via `task run`).
3. Change `AI_AGENT_CHANNEL_NAME` to your own AI Agent channel name.
4. Configure WIFI SSID and Password.
6. Rebuild and flash to the chip.

Notes:

1. Make sure at least one speaker is connected to the development board.
2. The server-side agora task is already running.

---
### Build Firmware

Copy this example directory (esp32-client) into the Espressif/frameworks directory. Run:

```bash
$ cd ../esp32-client
$ idf.py set-target esp32s3
$ idf.py menuconfig  --> Agora Demo for ESP32 --> (configure WIFI SSID and Password)
$ idf.py build
```

Configure FreeRTOS forward compatibility:
In menuconfig: Component config --> FreeRTOS --> Kernel, enable `configENABLE_BACKWARD_COMPATIBILITY`.

---
### Flash Firmware

Run:

```bash
$ idf.py -p /dev/ttyUSB0 flash monitor
```

Note: On Linux, you may encounter permission issues with `/dev/ttyUSB0`. Run `sudo usermod -aG dialout $USER`.

After a successful flash, the example will run automatically. Once the device joins the RTC channel, you will see the serial log:
`"Agora: Press [SET] key to Join the Ai Agent ..."`.

## FAQ

### Q1: `i2c driver install error` during compilation

**Cause**: I2C driver conflict—old and new APIs are both used.

**Solution**:
- Ensure `aic3104_ng.c` uses `driver/i2c.h` (old API).
- Ensure `driver/i2c_master.h` (new API) is not used.
- Check whether the code calls `i2c_driver_delete()` to delete the old driver.

### Q2: Runtime I2C timeout `ESP_ERR_TIMEOUT`

**Possible causes**:
1. Hardware wiring issues
2. Incorrect pin configuration
3. Incorrect I2C address
4. Missing pull-up resistors

**Debug steps**:
1. Check the I2C scan result in logs:
   ```
   W (xxxx) AIC3104_NG: Scanning I2C bus...
   W (xxxx) AIC3104_NG: Found device at address 0x??
   ```
2. If no device is detected, check the wiring.
3. If a device is detected but the address is not 0x18, modify `AIC3104_ADDR` in `aic3104_ng.h`.

### Q3: Compilation error `audio_board_init` not defined

**Cause**: Related code in `audio_proc.c` wasn’t properly commented out.

**Solution**:
Ensure all code that calls `board_handle` in the `setup_audio()` function is commented out.

### Q4: No audio output

**Possible causes**:
1. AIC3104 initialization failed
2. Incorrect I2S pin configuration
3. Audio data path issues

**Debug steps**:
1. Check logs to confirm AIC3104 initialization succeeded:
   ```
   AIC3104 detected, page register = 0x00
   ~~~~~AIC3104 Codec initialized successfully~~~~
   ```
2. Confirm the I2S pin configuration in `board_pins_config.c` is correct.
3. Use an oscilloscope to check whether the I2S signal lines have waveforms.

### Q5: Network buffer error `Not enough space`

**Symptom**:
```
[7479.598][err] -1/12:Not enough space
[7480.643][err][][netc_send_udp_data:185] 28 calls suppressed
```

**Cause**: This is a runtime network issue and does not affect hardware initialization.

**Solution**:
1. / (`idf.py menuconfig`)
2. Check network quality
3. Reduce audio bitrate

### Q6: Errors still occur after modifying `board_pins_config.c`

**Cause**: The file path modified is incorrect, or ESP-ADF was not rebuilt.

**Solution**:
1. Confirm you modified the correct file path (use `echo $ADF_PATH` or `echo %ADF_PATH%` to check).
2. Run `idf.py fullclean` for a full clean.
3. Rebuild with `idf.py build`.

---

## Verification

### Successful Startup Log

If everything is configured correctly, you should see logs similar to:

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

### Key Success Indicators

- ✅ `WiFi connected` - WiFi connected successfully
- ✅ `got ip: xxx.xxx.xxx.xxx` - IP address obtained
- ✅ `Found device at address 0x18` - AIC3104 chip detected
- ✅ `AIC3104 detected, page register = 0x00` - AIC3104 probe successful
- ✅ `AIC3104 Codec initialized successfully` - Codec initialization successful
- ✅ `agora_rtc_join_channel success` - RTC joined channel successfully

---

## Summary

### Core Adaptation Points

1. **Codec driver replacement**: Replace ES8311/ES7210 with AIC3104
2. **I2C API compatibility**: Use the old I2C API to avoid driver conflicts
3. **Pin remapping**: Modify the Board-layer configuration to adapt to new hardware
4. **Skip Board initialization**: Initialize AIC3104 directly without relying on the ESP-ADF Board layer

### Modified File Count

- **New files**: 2
- **Modified project files**: 3
- **Modified framework files**: 1 (ESP-ADF)

### Known Limitations

1. **Volume control**: Temporarily disabled; needs to be implemented via AIC3104 registers
2. **MCLK**: Disabled; can be enabled and pin-configured if needed
3. **Network buffers**: You may see buffer-insufficient warnings at runtime

### References

- [ESP-IDF Programming Guide](https://docs.espressif.com/projects/esp-idf/zh_CN/v5.2.3/esp32s3/)
- [ESP-ADF Programming Guide](https://docs.espressif.com/projects/esp-adf/zh_CN/latest/)
- [TI AIC3104 Datasheet](https://www.ti.com/product/TLV320AIC3104)
- [ReSpeaker Reference Project](https://github.com/zhannn668/esp32-client-respeaker)

---

## License

This adaptation guide is based on the MIT license of the TEN Framework.

---

**Document Version**: 1.0
**Creation Date**: 2025-01-05
**Applicable Hardware**: ReSpeaker XVF3800 + ESP32-S3
**Applicable Software**: TEN Framework esp32-client + ESP-IDF 5.2.3 + ESP-ADF 2.7

## About Agora

Agora Audio & Video IoT platform solutions rely on Agora’s self-built underlying real-time transport network, Agora SD-RTN™ (Software Defined Real-time Network), providing audio and video [...] for all Linux/RTOS devices that support networking.

## Technical Support

Please use the links below to obtain technical support:

- If you find bugs in the sample code or have other questions, you can contact the community lead directly.

We will reply as soon as possible.
