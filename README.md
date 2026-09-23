# 非遗体验安全放行

本项目用于整理非遗体验安全放行领域中的事件名称、交换字段与脱敏样例，方便业务、运营和研发人员在同一套术语下讨论后续服务。资料只包含领域约定，不包含真实个人信息、生产连接或外部账号。

## 目录

- `src/`：事件种类与最小字段校验。
- `data/sample.json`：用于核对资料格式的虚构事件。
- `tests/`：保证样例与领域约定保持一致。

## 本地核对

```bash
python3 -m unittest discover -s tests
```

## 本地运行

测试命令：

```bash
python3 -m unittest discover -s tests
```

编译或构建命令：

```bash
python3 -m compileall -q .
```

所有测试和构建均在单个 Linux 应用容器内完成，不需要另行启动数据库或外部服务。
