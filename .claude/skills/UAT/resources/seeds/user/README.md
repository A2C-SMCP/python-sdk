# `user/` — 就地 DropIn 种子

> User 源 SKILL **不复制进 SKILL Home**——它就地发现。本目录的种子是"现成的可拷贝
> 资产"，acceptance 期间被拷进临时 `$A2C_SKILL_HOME/user/<basename>/` 或
> `<workdir>/.tfrobot/skills/<basename>/`。

## 子目录形态

### 单源

```
user/<name>/
├── _seeds.manifest   ← 派生 _common/<x>
├── README.md
└── acceptance.md     ← 含 bash 自动化片段 + 手动确认项
```

### 多源对比（如未来 override-low-vs-high）

```
user/<name>/
├── home-user/<skill>/
├── workdir-1/<skill>/
├── workdir-2/<skill>/
├── README.md
└── acceptance.md
```

## name vs frontmatter.name

user 源 SKILL 的 ID = **目录 basename**（设计 §5.0）。失败种子如
`invalid-name-camelcase/` 用 **camelCase 目录名** 触发 `SkillNameError`，而不是改
frontmatter `name`。

## 子目录与文件

```
user/
├── _helpers/
│   └── run_user_staging.py  ← 驱动 stage_user_skills 的最小 driver
└── <name>/                   ← 种子目录
```

## 索引

参见上级 [`seeds/README.md`](../README.md) `user/` 节。

## 详细规范

[`uat-seed/resources/recipes/user.md`](../../../../uat-seed/resources/recipes/user.md)
