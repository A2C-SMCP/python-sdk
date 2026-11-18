# 升级迁移：恢复被 0.5.0 旧卸载行为删除的 marketplace catalog 子树

> 适用版本：从 `0.5.0`（含 `0.5.0a*` 预发布）升级到 `0.5.1`
> 影响对象：曾卸载过**相对 source** 插件的 marketplace
> 性质：手工恢复说明。SDK 不提供自动迁移命令，按本文操作即可；不涉及协议与跨端行为。

## 1. 背景与影响面

0.5.0 的 `plugin uninstall` / `plugin gc` 会删除插件的安装目录。对 `source` 为相对路径的插件
（marketplace 清单中的 `"source": "./plugin"`），该目录位于 marketplace 的**共享** clone 内：

```text
<A2C_SKILL_HOME>/marketplace/<marketplace>/<rel_path>
```

因此，卸载一个插件会连带删除同一子树下的父子 / 同源兄弟插件的内容，也会删掉其它 scope 仍在使用的内容；
此后再次安装该插件会报 `plugin root not found after resolve: …`。

0.5.1 修复了**未来**的行为：卸载只删除插件独占的外部 clone
（`<A2C_SKILL_HOME>/marketplace/.plugins/<marketplace>/<plugin>`），共享 catalog 子树作为 marketplace 级缓存保留，
统一归 `marketplace remove` / `prune` 清理。

**本文处理的是已经损坏的本地状态。** 损坏只发生在本机 clone，上游仓库无损——重新 clone 一份即可完整恢复。

同时满足以下三条的环境受影响：

1. 使用过 0.5.0（含 `0.5.0a*` 预发布）；
2. 卸载过（或 `plugin gc` 清理过）`source` 为相对路径的插件；
3. 该 marketplace 的本地 clone 仍在原处（未在升级后重新 `marketplace add` 过）。

不受影响：

- `source` 为 `url` / `github` / `cnb` / `git-subdir` 的插件——它们有自己的外部 clone，重装时会重新 clone；
- 从未卸载过插件的环境；
- 已经重新 `marketplace add` 过的 marketplace（clone 已是干净副本）。

## 2. 如何判断自己受影响

任一症状即可确认：

- `plugin enable <plugin>@<marketplace>` 失败，提示
  `cannot enable plugin …: its installed content is missing or unusable`，并附带 `Recovery: …` 指引；
- `plugin install <plugin>@<marketplace>` 失败，错误文本包含 `plugin root not found after resolve`
  （当前版本可能以未处理异常、带堆栈的形式打印，不影响按本文恢复）；
- 直接检查：`<A2C_SKILL_HOME>/marketplace/<marketplace>/<rel_path>` 已不存在，而 `plugin list` 中该插件仍在册。

## 3. 恢复步骤

核心思路：**丢弃本地 catalog clone，重新 clone 一份干净副本，再重装受影响的插件。**

```bash
# 1) 移除受影响 marketplace：默认级联卸载其下所有已安装插件，并撤销该 marketplace 的 trust
a2c-computer marketplace remove <marketplace-name>

# 2) 重新添加（重新 clone）。非交互执行时必须带 --trust。
#    #260 起注册名**取清单顶层 name**（不再从 URL 派生、--name 也不再是别名——它只作期望名校验）。
#    若上一步移除前的名字与清单名不同（legacy 注册），重新 add 会得到清单名，插件 id 的 @ 段要按新名改用。
a2c-computer marketplace add <git-url> --trust

# 3) 逐个重装并重新启用
a2c-computer plugin install <plugin>@<marketplace-name>
a2c-computer plugin enable  <plugin>@<marketplace-name>
```

说明：

- 第 1 步需要完整 marketplace 名（`a2c-computer marketplace list` 可查）。
- 第 2 步的 `<git-url>` 用该 marketplace 原来的 source（`a2c-computer marketplace info <name>` 在移除前可查）。
  **v0.5.1（#260）起注册名 = 清单顶层 `name`**：若移除前看到的名字与清单名不同（URL 派生名或当初 `--name`
  指定的别名，属 legacy 注册），重新 `add` 后 marketplace 会注册成**清单名**，后续 `plugin install/enable`
  的 `@<marketplace>` 段要相应改用清单名。存量注册的统一迁移见 #268。
- `plugin install` **只安装不激活**，所以每个插件都要再 `plugin enable` 一次，才能恢复 SKILL 投影与插件携带的
  MCP server。
- 第 3 步对每个受影响的插件重复执行。

## 4. 破坏性警告

- `marketplace remove` **默认级联**：卸载该 marketplace 下**所有**已安装插件（删除安装意图、启用状态、SKILL 投影
  与插件携带的 MCP server 配置），并**撤销该 marketplace 的 trust**。恢复后需要重新 `plugin enable` 才能回到
  原先可用状态。
- **不要使用 `--keep-plugins`。** 它只删除 clone（并撤销 trust），却保留 installed 状态与账本记录，留下指向已删除
  clone 的孤儿记录（`plugin list` 仍会列出这些插件），后续恢复更麻烦。恢复场景请使用默认行为。
- 若输出 `failed to uninstall N plugin(s)` 且退出码为 1：marketplace 已被移除，但这 N 个插件的账本可能仍有残留；
  重新 `add` 后按提示逐个重装即可。
- 其它 marketplace 与其插件不受影响。

## 5. 为什么 `refresh` / 重启不能自愈

- `marketplace refresh <name>` 只对 catalog clone 执行 `git pull --ff-only`。上游未变时 pull 结果是 up-to-date，
  **不会**还原本地已删除的文件；只有 pull 失败时才会全量重新 clone。单个插件 staging 失败只记 ERROR 并继续，
  刷新汇总仍可能显示 `0 failed`、退出码 0——**不要**把它当成已恢复的信号。
- `plugin install` 默认复用既有 clone（不做 pull）。
- 启动时的重物化（boot rematerialize）只在账本记录的安装根**整体缺失或不可解析**时触发，且同样复用既有 clone，
  无法把已删除的文件取回来。

因此唯一可靠的恢复路径是让 marketplace 重新 clone 一份干净副本，即第 3 节。

## 6. 恢复后确认

```bash
a2c-computer plugin list                     # 插件在册、状态符合预期
a2c-computer plugin info <plugin>@<marketplace-name>
ls <A2C_SKILL_HOME>/marketplace/<marketplace-name>/<rel_path>   # 相对源子树已恢复
```

确认无误后即可正常使用。0.5.1 起再次卸载这些插件不会删除共享 catalog 子树，不需要重复本文步骤。
