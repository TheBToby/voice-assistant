idf_component_register(SRCS "main.c" "example.c" "board.c" "media.c"
                           "wake_word.c" "wake_word_engine_esp_sr.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES esp_wifi)