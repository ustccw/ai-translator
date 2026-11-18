# ai-translator

在 Ubuntu 桌面上划选文字，按快捷键，让模型给出译文或润色，再在原处替换选区。省掉「复制、切到网页、粘贴、再复制回来」这几步。

只支持 GNOME 的 X11 会话。Wayland 不能用。登录界面选「Ubuntu on Xorg」。确认当前会话：

```bash
echo "$XDG_SESSION_TYPE"
```

输出应为 `x11`。

## 安装

在仓库目录执行：

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 python3-xlib xdotool xclip
chmod +x ./ai-translator
```

用 Cursor 时再装本地 SDK：

```bash
pip3 install --user cursor-sdk
```

用 DeepSeek 时不需要这个包。

## 配置

程序读仓库里的 `config.toml`。改完保存，下次按快捷键生效。API key 不写在这个文件里。

`provider` 决定用哪个模型：

```toml
provider = "deepseek"   # 或 "cursor"
```

DeepSeek 走 `https://api.deepseek.com` 的对话接口，一般一两秒返回。默认模型是 `deepseek-flash`，`thinking = false` 表示关闭思考模式。Cursor 使用本机 Agent，第一次大约要半分钟，之后在 `agent_idle_sec`（默认 15 分钟）内再译会快一些。

密钥各放一个文件，一行，不要引号，权限 `0600`：

```bash
mkdir -p ~/.config/ai-translator
chmod 700 ~/.config/ai-translator
```

| 后端 | 文件 | 配置项 |
| --- | --- | --- |
| DeepSeek | `~/.config/ai-translator/deepseek_key` | `[deepseek] api_key_file` |
| Cursor | `~/.config/ai-translator/api_key` | `[cursor] api_key_file` |

用编辑器写入密钥后执行 `chmod 600` 对应文件。

## 注册快捷键

```bash
./ai-translator install-shortcuts
```

默认把下面两个键写进 GNOME，已有的自定义快捷键会保留。快捷键里记录的是 `ai-translator` 的绝对路径。把仓库挪到别的目录后，再执行一次上面的命令。

| 按键 | 作用 |
| --- | --- |
| Super+T | 中文和英文对翻 |
| Super+I | 同一种语言润色，不翻译 |

改了 `config.toml` 里的快捷键后，再执行一次上面的命令。

## 使用

1. 划选文字。200 字以内给出三条候选（礼貌、自然、精炼），超过 200 字只给一条「自然」。超过 4000 字直接取消。
2. 按 Super+T 或 Super+I。浮窗不抢走当前窗口的焦点。
3. 第一次使用时，浮窗会说明选中的文字会发到配置的模型接口。Enter 继续，Esc 取消。确认后不再询问。
4. 在可以写回的窗口里，按数字键选一条。只有一条时也可以按 Enter。选区会被这条文字盖住。Esc 关闭，不改原文。
5. 粘贴后原来的剪贴板会放回去。写错了用目标程序自己的撤销，一般是 Ctrl+Z。

中文译成英文时，按同事聊天的口气改成礼貌、简单、直接的英文，事实、命令、数字保持原样。英文译成中文时直接翻译。

终端和 PDF 是只读窗口：Super+T 只把英文译成中文并显示，不写回；选区已经是中文，或者按 Super+I，都没有任何反应。终端里也不会发送 Ctrl+C。名单以外的窗口同样只读。

编辑器、Office、Teams，以及 Chrome、Firefox 等已列入 `config.toml` 的程序可以互译、润色并写回。浏览器整窗只有一个类名，输入框和网页正文分不开。在输入框里划选可以写回；划选的是网页正文时，按数字键会向当前焦点发送 Ctrl+V，正文本身通常收不进去。

某个程序没有反应时，用 `xprop WM_CLASS` 点一下那个窗口，把类名加进 `config.toml` 的 `editable_wm_class`。终端不要加进去。其余可调项在 `config.toml` 里有中文注释。
