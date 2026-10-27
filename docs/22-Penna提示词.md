# Penna 提示词能力（Bot 轻量移植）

> 上游：[storyAura/Penna](https://github.com/storyAura/Penna)（MIT）
> Penna 是 **Windows Flutter 桌面端的文生图提示词工作台**，不是 OneBot / NapCat 插件，
> 不能在 Ubuntu Bot 容器里直接运行 GUI。这里移植的是它最核心的“中文需求 → Danbooru/SD 提示词”能力。

## 用法

所有群都可以用：

```text
/penna 雨夜霓虹街头的白发少女，低机位，赛博朋克
/画图提示词 猫耳女仆在咖啡店，暖光，厚涂
/提示词 机甲的驾驶舱特写，电影感
/sd 一只戴围巾的橘猫，冬天，胶片感
```

输出三段：

1. 【正向提示词】Danbooru 风格英文标签；
2. 【负向提示词】常用质量/结构负面标签；
3. 【中文说明】构图说明和可调整项。

也可以自然语言让模型调用 `penna_prompt` 工具；模型会自动生成，不需要记命令。

## 边界

- **只生成提示词，不直接出图**；要出图需要另接 Stable Diffusion / NovelAI / ComfyUI。
- 不生成露骨色情、未成年性化、真实人物隐私/身份、仇恨暴力内容；遇到会改成安全版本。
- 群级 15 秒冷却，命令消耗计入群预算；不会无限刷。

## 如果要用完整 Penna

完整 Penna 的 UI、角色库、本地反推/翻译/标签订阅不在 Bot 里。可选方案：

1. **Windows 桥接**：在 Windows 上运行 Penna，把它的结果通过 HTTP/MCP/文件同步给 Bot；
   需要额外写桥接服务，且要处理安全鉴权。
2. **只同步 Skill/提示词模板**：把 Penna 的 `skills/*.md`、角色/风格模板导入 Bot 的提示词库；
   适合“统一画风/角色设定”，但不是完整 GUI。
3. **重写成 Linux headless 服务**：工作量最大，需要把 Flutter UI 与核心引擎拆开。

当前 Bot 采用方案 1 的轻量替代：直接复用大模型生成提示词，所有群可用，不依赖 Windows。
