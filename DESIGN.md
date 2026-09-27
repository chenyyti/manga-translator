---
name: 漫画智能翻译
description: 面向本地漫画制作的出版编辑台
colors:
  ink: "#233239"
  muted: "#52636a"
  paper: "#fffdf8"
  canvas: "#f1eee7"
  line: "#d8d9d2"
  line-strong: "#a8b5b9"
  accent: "#285e78"
  accent-dark: "#18475f"
  proof: "#ad493b"
  success: "#286a53"
  warning: "#846015"
  danger: "#a53e3a"
  topbar: "#faf9f5"
  selection: "#c4dae4"
  status-base: "#eef0f2"
  status-success: "#e8f4ef"
  status-warning: "#fdf1df"
  status-danger: "#fbe9e7"
typography:
  headline:
    fontFamily: "STSong, SimSun, Songti SC, serif"
    fontSize: "clamp(1.85rem, 3vw, 2.7rem)"
    fontWeight: 750
    lineHeight: 1.17
    letterSpacing: "0.005em"
  body:
    fontFamily: "Segoe UI, Microsoft YaHei UI, Microsoft YaHei, PingFang SC, sans-serif"
    fontSize: "0.94rem"
    lineHeight: 1.55
  navigation:
    fontSize: "0.9rem"
    fontWeight: 650
  status:
    fontSize: "0.76rem"
    fontWeight: 700
rounded:
  brand: "5px"
  control: "7px"
  option: "9px"
  surface: "12px"
  pill: "999px"
spacing:
  nav-gap: "28px"
  card-gap: "12px"
  page-gutter: "24px"
  mobile-gutter: "14px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "#fff"
    rounded: "{rounded.control}"
    height: "42px"
    padding: "0 20px"
  button-primary-hover:
    backgroundColor: "{colors.accent-dark}"
  navigation-item:
    textColor: "{colors.muted}"
  navigation-item-active:
    textColor: "{colors.ink}"
  status-pill:
    backgroundColor: "{colors.status-base}"
    rounded: "{rounded.pill}"
    padding: "2px 10px"
    height: "26px"
  project-card:
    backgroundColor: "{colors.paper}"
    rounded: "{rounded.surface}"
  source-mode:
    backgroundColor: "#fff"
    rounded: "{rounded.option}"
    padding: "16px"
---

# Design System: 漫画智能翻译

## Overview

**Creative North Star: "出版编辑台"**

界面像一张正在校对的工作桌：暖白纸面承载项目资料，深墨色让中文信息清楚可读，蓝色指出当前操作与焦点，朱红色提示需要核对的内容。真正的漫画封面、缩略图和成品图是视觉主角。

这是供 Windows 本地浏览器长期使用的制作工作台。页面标题直接进入内容，任务状态和可执行动作靠近素材出现；视觉秩序服务于逐页检查和完成制作。字体使用系统和本机中文后备栈，运行时不依赖远程字体或装饰素材。

**Key Characteristics:**

- 暖纸底、深墨字、克制的编辑蓝与朱红校对提示。
- 标题采用宋体后备栈，正文采用本机无衬线后备栈。
- 静态卡片主要靠边线和底色分层，交互时才轻微抬升。
- 漫画图像优先；处理状态同时用文字、形状和颜色表达。

## Colors

页面底色是暖灰纸张，内容面是更亮的暖白。蓝色用于操作和选中状态；朱红 `proof` 标示未保存编辑与已过期的 OCR 内容，不代替明确的状态文字。

### Primary

- **编辑蓝** (`#285e78`, `--accent`): 主按钮、链接、选中边框、进度与当前步骤。
- **深编辑蓝** (`#18475f`, `--accent-dark`): Element Plus 主色深态。

### Secondary

- **校对朱红** (`#ad493b`, `--proof`): 未保存编辑提示与已过期 OCR 提示。

### Neutral

- **深墨** (`#233239`, `--ink`): 主文字及深色占位封面。
- **旁注灰** (`#52636a`, `--muted`): 次要说明、元数据和未选中的导航。
- **纸面** (`#fffdf8`, `--paper`): 项目卡片及主要内容容器。
- **桌面** (`#f1eee7`, `--canvas`): 页面背景。
- **细线** (`#d8d9d2`, `--line`) 与 **强调线** (`#a8b5b9`, `--line-strong`): 边框和分隔。
- **页眉纸色** (`#faf9f5`): 顶部导航背景。

状态另有成功绿 `#286a53`、警示棕 `#846015`、危险红 `#a53e3a`，对应状态胶囊底色分别为 `#e8f4ef`、`#fdf1df`、`#fbe9e7`；普通状态底色为 `#eef0f2`。

## Typography

**Display Font:** `STSong, SimSun, Songti SC, serif`，用于页面和工作台主标题。  
**Body Font:** `Segoe UI, Microsoft YaHei UI, Microsoft YaHei, PingFang SC, sans-serif`。  
**Label/Mono Font:** `ui-monospace, monospace` 用于步骤编号和技术性小数字；品牌方块及占位封面使用 `STKaiti, KaiTi, serif`。

### Hierarchy

- **Headline** (`750`, `clamp(1.85rem, 3vw, 2.7rem)`, `1.17`): 页面标题；工作台标题另收至 `clamp(1.5rem, 2.4vw, 2rem)`。
- **Body** (`0.94rem`, `1.55`): 长时间阅读的常规界面文字。
- **Navigation** (`650`, `0.9rem`): 顶部导航项。
- **Status** (`700`, `0.76rem`): 状态胶囊，保留文字标签。

## Layout

通用页面使用最大宽度 `1180px`，左右各至少 `24px` 留白；项目工作台主要区域最大宽度 `1280px`。顶部导航高 `72px`，内容直接从标题和主要操作展开。项目卡片以封面和信息并置，工作台依次呈现步骤、缩略图、图像比较与区域编辑。

在 `900px` 以下，主导航固定到视口底部，双栏表单和设置区折为单栏；`767px` 以下，图像比较改用分段切换；`620px` 以下，页面横向留白为 `14px`，缩略图、工具和项目操作可折叠。工作台编辑区在 `1200px` 以上使用三栏，在较窄桌面使用双栏。

## Elevation & Depth

默认容器由纸面、细边线和桌面底色区分。项目卡片在悬停或内部聚焦时使用 `0 8px 22px rgb(35 50 57 / 9%)` 阴影并上移 `1px`；画布里的图像另有较深的阴影，使成品与暗色校对台分离。全局焦点轮廓为 `3px solid #417e9c`，偏移 `3px`。

## Shapes

主要卡片和空状态容器为 `12px` 圆角；操作按钮的 Element Plus 基础半径为 `7px`；选项卡为 `9px`；状态胶囊为 `999px`。封面图裁切在卡片边界内，漫画画布上的图像仅有 `2px` 小圆角。边框通常是 `1px` 实线。

## Components

### Buttons

主操作使用编辑蓝填充和白字。顶部“新建项目”按钮至少高 `42px`，水平内边距 `20px`，圆角 `7px`。通用键盘聚焦保留明显的蓝色轮廓。次级项目操作可采用透明背景与灰字，悬停时转为编辑蓝。

### Chips

状态胶囊至少高 `26px`，有 `2px 10px` 内边距和圆形小点。成功、警示、危险与处理中各有底色和文字色；标签文字始终说明真实状态。

### Cards / Containers

项目卡片为 `12px` 圆角、`1px` 细线、暖白纸面，列表样式高 `138px`、左侧封面列宽 `96px`。悬停和内部聚焦时边框变蓝并轻微抬升。阅读卡片改为上方大封面、下方信息。

### Inputs / Fields

表单依托 Element Plus 的本地组件样式，基础控件圆角为 `7px`、基础字级为 `14px`。来源模式是可点选的 `9px` 圆角边框卡片；悬停或选中时，编辑蓝边线与内描边共同表明状态。

### Navigation

桌面顶部导航项使用旁注灰字，当前项改为深墨并显示 `2px` 蓝色下划线；宽度 `900px` 以下转为底部固定导航。设置页的局部导航在桌面为侧栏，在窄屏转为网格。

## Do's and Don'ts

### Do:

- **Do** 让真实漫画封面、缩略图和成品图占据视觉重点。
- **Do** 用编辑蓝标记当前动作、选中边框和焦点。
- **Do** 在状态颜色旁保留明确中文标签，并把错误原因放在操作附近。
- **Do** 使用本机字体后备栈和已有的 CSS 变量。

### Don't:

- **Don't** 以远程运行时字体或装饰素材建立页面识别度。
- **Don't** 只靠颜色传达处理结果、失败或不可逆操作。
- **Don't** 用大段功能介绍挤走首屏的内容和当前动作。
