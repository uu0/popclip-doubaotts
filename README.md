# DoubaoSay for PopClip

用火山引擎豆包语音合成朗读选中文本的 [PopClip](https://www.popclip.app/) 扩展。

选中任意文本 → 弹出 PopClip 工具条 → 点击"豆包朗读"→ 云端合成中文语音并播放。

## 兼容性

- 使用旧版 JSON 格式配置（`Config.json` + `popclipVersion 4069`），兼容 **PopClip 2023.9**（买断版）及之后版本。
- 仅依赖 Python 3 标准库（`urllib`/`ssl`/`socket` 手写 WebSocket 客户端），macOS 自带 Python 3 即可运行，无需 `pip` 安装任何依赖。

## 安装

1. 前往 [Releases](../../releases) 下载 `DoubaoSay.popclipextz`。
2. 双击安装（PopClip 会提示安装扩展）。
3. 去火山引擎控制台开通**语音合成**服务，获取 API Key（`seed-tts-2.0` 资源）。
4. 在 PopClip 设置 → 扩展 → 豆包朗读 → 选项中填写 **API Key**。

## 选项

| 选项 | 说明 |
| --- | --- |
| API Key | 火山引擎语音合成的 API Key。填写后优先使用；留空则回落到 macOS 钥匙串 |
| 音色 | 12 个 `seed-tts-2.0` 中文音色下拉选择（默认：爽快思思 2.0） |
| 语速 | 语速倍率，如 `1.0`；留空为 1.0（映射到 `speech_rate` -50～100） |

### 备选：钥匙串方式

如果不想把 Key 填在扩展设置里，可以只存 macOS 钥匙串（扩展设置的 API Key 留空即可）：

```bash
security add-generic-password -s popclip-doubaotts -a api_key -w '你的API_KEY'
```

验证是否写入成功：

```bash
security find-generic-password -s popclip-doubaotts -a api_key
```

更新已存的 Key（加 `-U`）：

```bash
security add-generic-password -U -s popclip-doubaotts -a api_key -w '新的API_KEY'
```

## 技术要点

- 火山引擎《WebSocket 单向流式 - V3》协议：`wss://openspeech.bytedance.com/api/v3/tts/unidirectional/stream`，`X-Api-Key` 鉴权。
- 长文本按段落切分（每块 ≤ 4000 字）逐块合成后拼接播放。
- 合成结果按文本哈希缓存在 `~/Library/Caches/popclip-doubaotts/`，相同文本不再重复请求。
- 点击 PopClip 的 spinner 可取消：脚本捕获 SIGTERM/SIGINT，杀掉 `afplay` 并清理临时文件。
- 运行日志写入 `~/Library/Logs/popclip-doubaotts/debug.log`（**绝不记录 Key 明文**），出问题时先看它。

## 故障排查

- **转圈后显示 X**：多半是 API Key 无效或未开通语音合成服务。检查 Key 是否正确、控制台是否已开通 `seed-tts-2.0` 资源，再看上面的 debug 日志。
- **`Failed to unzip extension`**：下载的 `.popclipextz` 不完整。删除后重新下载，对一下文件大小再安装。

## 打包（开发者）

`.popclipextz` 必须是**包含** `.popclipext` 文件夹的 zip（而不是把文件平铺在 zip 根目录），且**文件夹名要与 zip 基名一致**，否则 PopClip 安装时报 `Failed to unzip extension`：

```bash
cd DoubaoSay-oss
zip -r DoubaoSay.popclipextz DoubaoSay.popclipext
```

注意：不要把 `__pycache__/` 打进包里。

## License

MIT
