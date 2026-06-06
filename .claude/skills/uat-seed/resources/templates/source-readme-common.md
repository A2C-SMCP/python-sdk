# `_common/` — SKILL 包原料库

> SKILL 包内容的**单一定义源**。`mcp/` / `marketplace/` / `user/` 在 setup 阶段从这里
> 派生（拷贝或打包），保证三源 SKILL 包内容一致。

## 子目录

每个 `<name>/` 是一份完整的 SKILL 包（合法或非法）。具体规范见
[`uat-seed/resources/recipes/common.md`](../../../uat-seed/resources/recipes/common.md)。

## 索引（按 axis）

参见上级 [`seeds/README.md`](../README.md) `_common/` 节。

## 改动须知

修改 `_common/<name>/` 的任何内容后**必须**：

1. 重新跑该目录 `acceptance.md`
2. 重跑 README 内"派生引用方"列出的所有 mcp / marketplace / user 种子的 acceptance
3. 如 `_archives/` 引用了本目录，重建 `_archives/build.sh` 并比对 `manifest.json`
