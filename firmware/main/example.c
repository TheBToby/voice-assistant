#include "esp_log.h"
#include <string.h>

#include "livekit.h"

#include "media.h"
#include "board.h"
#include "example.h"
#include "sdkconfig.h"
#include "voice_session.h"

static const char *TAG = "livekit_example";

static livekit_room_handle_t room_handle;
static volatile bool room_connected;

/// Invoked when the room's connection state changes.
static void on_state_changed(livekit_connection_state_t state, void* ctx)
{
    ESP_LOGI(TAG, "Room state changed: %s", livekit_connection_state_str(state));

    room_connected = (state == LIVEKIT_CONNECTION_STATE_CONNECTED);
    voice_session_set_room_connected(room_connected);

    livekit_failure_reason_t reason = livekit_room_get_failure_reason(room_handle);
    if (reason != LIVEKIT_FAILURE_REASON_NONE) {
        ESP_LOGE(TAG, "Failure reason: %s", livekit_failure_reason_str(reason));
    }
}

/// Invoked for data packets from remote participants (agent events).
static void on_data_received(const livekit_data_received_t *data, void *ctx)
{
    (void)ctx;
    if (data->topic != NULL &&
        strcmp(data->topic, CONFIG_LK_AGENT_EVENT_TOPIC) == 0) {
        // Payload is a NUL-terminated JSON document (agent protocol).
        voice_session_on_agent_event((const char *)data->payload.bytes,
                                     data->payload.size);
    }
}

bool example_room_connected(void)
{
    return room_connected;
}

const char *example_failure_reason(void)
{
    if (room_handle == NULL) {
        return NULL;
    }
    livekit_failure_reason_t reason =
        livekit_room_get_failure_reason(room_handle);
    if (reason == LIVEKIT_FAILURE_REASON_NONE) {
        return NULL;
    }
    return livekit_failure_reason_str(reason);
}

bool example_publish_event(const char *json)
{
    if (room_handle == NULL || !room_connected || json == NULL) {
        return false;
    }
    livekit_data_payload_t payload = {
        .bytes = (uint8_t *)json,
        .size = strlen(json),
    };
    livekit_data_publish_options_t options = {
        .payload = &payload,
        .topic = (char *)CONFIG_LK_DEVICE_EVENT_TOPIC,
        .lossy = false,
    };
    return livekit_room_publish_data(room_handle, &options) == LIVEKIT_ERR_NONE;
}

void join_room()
{
    if (room_handle != NULL) {
        ESP_LOGE(TAG, "Room already created");
        return;
    }

    livekit_room_options_t room_options = {
        .publish = {
            .kind = LIVEKIT_MEDIA_TYPE_AUDIO,
            .audio_encode = {
                .codec = LIVEKIT_AUDIO_CODEC_OPUS,
                .sample_rate = 16000,
                .channel_count = 1
            },
            .capturer = media_get_capturer()
        },
        .subscribe = {
            .kind = LIVEKIT_MEDIA_TYPE_AUDIO,
            .renderer = media_get_renderer()
        },
        .on_state_changed = on_state_changed,
        .on_data_received = on_data_received,
    };
    if (livekit_room_create(&room_handle, &room_options) != LIVEKIT_ERR_NONE) {
        ESP_LOGE(TAG, "Failed to create room");
        return;
    }

    // Self-hosted server with a pre-minted access token
    // (repo root: make token ID=respeaker-1 ROOM=home).
    ESP_LOGI(TAG, "Connecting to %s", CONFIG_LK_EXAMPLE_SERVER_URL);
    if (livekit_room_connect(
            room_handle,
            CONFIG_LK_EXAMPLE_SERVER_URL,
            CONFIG_LK_EXAMPLE_TOKEN) != LIVEKIT_ERR_NONE) {
        ESP_LOGE(TAG, "Failed to connect to room");
    }
}

void leave_room()
{
    if (room_handle == NULL) {
        ESP_LOGE(TAG, "Room not created");
        return;
    }
    room_connected = false;
    voice_session_set_room_connected(false);
    if (livekit_room_close(room_handle) != LIVEKIT_ERR_NONE) {
        ESP_LOGE(TAG, "Failed to leave room");
    }
    if (livekit_room_destroy(room_handle) != LIVEKIT_ERR_NONE) {
        ESP_LOGE(TAG, "Failed to destroy room");
        return;
    }
    room_handle = NULL;
}
