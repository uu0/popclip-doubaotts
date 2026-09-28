#!/bin/zsh
# doubao_say.zsh — PopClip 入口：把 stdin 文本交给 Python 客户端做豆包 TTS 合成并播放。
# 认证信息从 macOS 钥匙串读取（service: popclip-doubaotts，account: api_key；
# 若设置中未填 API Key 则尝试钥匙串；若无 api_key 则回落旧版 account app_id / access_key）。
exec python3 "${0:A:h}/doubao_tts.py"
