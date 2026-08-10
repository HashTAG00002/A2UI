# docs/our_tex —— 论文源码目录（与 Overleaf 双向同步）

本目录下的 LaTeX 源码通过 `git subtree` 与一个 Overleaf 项目双向同步，
使写作 Agent 可以直接在代码库内（能看到 `taskvm/` 实现和 `eval_results/` 真实实验数据）
撰写论文，同时论文仍然能在 Overleaf 上正常渲染/协作/分享。

## 目录结构

```
docs/our_tex/
├── main.tex              # 论文主入口
├── sections/              # 各章节 .tex（\input 进 main.tex）
├── figures/                # 图表（应由 eval_results/ 自动生成，禁止手画/手抄数字）
├── refs.bib                # 参考文献
└── scripts/
    ├── compile.sh           # 本地编译（pdflatex+bibtex，四遍编译，不依赖 latexmk）
    └── sync_to_overleaf.sh  # 与 Overleaf 双向同步（git subtree push/pull）
```

## 一次性准备：获取 Overleaf Git Token

1. 登录 Overleaf，打开 [Account Settings](https://www.overleaf.com/user/settings)，
   找到 "Git Integration"，生成一个 Git 认证 token（用户名固定为 `git`，密码就是这个 token）。
   olp_8yDXXpLOEEmuE3PudSAx1F1Ti9FWlB1tlF2m
   https://www.overleaf.com/project/6a79a0df1e88b085b10abe09
2. 新建（或打开已有）一个 Overleaf 项目，记下项目 ID：
   项目 URL 形如 `https://www.overleaf.com/project/<PROJECT_ID>`。
3. 在本地 shell 里配置环境变量（**不要**把 token 写进本仓库任何会被提交的文件）：

   ```bash
   export OVERLEAF_PROJECT_ID="<你的项目ID>"
   export OVERLEAF_GIT_TOKEN="<你的Git token>"
   ```

   建议追加到 `~/.bashrc`，或者写入 `docs/our_tex/.env.local` 再 `source` 它——
   该文件已加入 `.gitignore`，不会被提交。

## 首次初始化 subtree（只需做一次）

```bash
source docs/our_tex/.env.local   # 如果用了这种方式管理变量
bash docs/our_tex/scripts/sync_to_overleaf.sh init
```

这会把 `docs/our_tex/` 目录的内容作为 subtree 推送到 Overleaf 项目的 `main` 分支，
之后打开 Overleaf 网页端项目即可看到同步过来的内容。

## 日常使用

- **Agent/你在本地改完论文后**，推送到 Overleaf 触发云端重新编译：

  ```bash
  bash docs/our_tex/scripts/sync_to_overleaf.sh push
  ```

- **在 Overleaf 网页端手动改了内容**（比如临时改了个公式、和合作者一起改），拉回本地：

  ```bash
  bash docs/our_tex/scripts/sync_to_overleaf.sh pull
  ```

- **push 之前先在本地验证能否编译通过**（避免把编译错误推到 Overleaf 上）：

  ```bash
  bash docs/our_tex/scripts/compile.sh
  ```

## 给写作 Agent 的约定

- 所有实验数字、图表必须来自 `eval_results/*.json` 的真实产出，不要臆造或估算。
- 方法描述应直接对应 `taskvm/` 下的真实实现（模块名、函数名可以直接引用）。
- 新增参考文献统一加到 `refs.bib`，优先使用可交叉验证来源（arXiv / ACL Anthology / DBLP 导出的 BibTeX）。
