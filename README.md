# 每日文献

每天早上 8 点(北京时间)自动抓取 arXiv 和指定天文期刊的新文献,按分类关键词筛选
,用 Claude 生成两三句中文摘要,发布成一个按类别分组的网页。
iPhone / Mac 打开即看。

## 部署步骤(一次性,约 10 分钟)

### 1. 建仓库
- 注册/登录 [github.com](https://github.com),点右上角 **+** → **New repository**
- 名字随意(如 `paper-daily`),选 **Public**(免费版 GitHub Pages 要求公开仓库)
- 点 **Create repository**

### 2. 上传文件
- 在新仓库页面点 **uploading an existing file**,把本文件夹里的所有内容拖进去,Commit
- 检查 `.github/workflows/daily.yml` 是否上传成功(隐藏文件夹有时拖不上去)。
  若没有:点 **Add file → Create new file**,文件名输入 `.github/workflows/daily.yml`,
  把本地该文件内容粘贴进去,Commit

### 3. 配置 API key(中文摘要用)
- 到 [console.anthropic.com](https://console.anthropic.com) 注册,在 **Billing** 充值(最低 $5,按本项目用量能用一年以上)
- 在 **API Keys** 页面创建一个 key,复制
- 回到 GitHub 仓库:**Settings → Secrets and variables → Actions → New repository secret**
  - Name 填 `ANTHROPIC_API_KEY`,Secret 粘贴你的 key,保存
- 不配置也能用,只是显示英文原摘要

### 4. 开启网页
- 仓库 **Settings → Pages**,Source 选 **Deploy from a branch**,
  Branch 选 `main`、文件夹选 `/docs`,Save

### 5. 首次运行
- 仓库顶部 **Actions** 标签 → 若提示启用 workflows 就点启用
- 左侧选 **每日文献更新** → 右侧 **Run workflow** 手动跑一次
- 跑完后访问 `https://你的用户名.github.io/仓库名/`

### 6. 手机 / Mac 上像 App 一样用
- **iPhone**:Safari 打开网址 → 分享按钮 → **添加到主屏幕**
- **Mac**:Safari 打开网址 → 文件菜单 → **添加到程序坞**

之后每天早上自动更新,打开就是最新内容。

## 日常使用

- **改关键词 / 分类 / 加期刊**:在 GitHub 网页上直接编辑 `config.yaml`(点文件 → 铅笔图标),
  Commit 后自动按新配置重跑。加期刊只需名称 + ISSN;加分类照抄现有格式即可
- **排除某方向**:把特征词加进 `exclude_title` 列表(只匹配标题,避免误伤)
- **手动刷新**:Actions 页面 Run workflow
- **重置历史**:删除 `data/history.json` 即可(存档会清空重来)
- 仓库自带的 `data/history.json` 是一份演示数据(2026-07-14 的三篇真实论文),可保留或删除

## 开销

GitHub 全免费。Claude API 按每天 5–20 篇摘要估算,每月约 $0.1–0.5。

## 结构

```
config.yaml                 # 关键词、分区、期刊、模型 —— 你唯一需要改的文件
scripts/build.py            # 抓取 arXiv/Crossref → 去重 → 中文摘要 → 生成网页
.github/workflows/daily.yml # 定时器:每天 UTC 00:00(北京 08:00)
docs/                       # 生成的网页(GitHub Pages 发布目录)
data/history.json           # 已推送过的文献,用于去重和存档
```
