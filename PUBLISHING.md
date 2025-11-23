# 发布到 PyPI 指南

## 1. 注册 PyPI 账号

### PyPI (生产环境)
- 访问：https://pypi.org/account/register/
- 注册一个账号
- 验证邮箱

### TestPyPI (测试环境，推荐先用这个测试)
- 访问：https://test.pypi.org/account/register/
- 注册一个账号（可以和PyPI使用不同的账号）
- 验证邮箱

## 2. 配置 API Token（推荐方式）

### 2.1 生成 API Token

**PyPI:**
1. 登录 https://pypi.org
2. 进入 Account settings → API tokens
3. 点击 "Add API token"
4. 设置名称（如 "mailbox-upload"）
5. 选择 Scope: "Entire account" 或特定项目
6. 复制生成的 token（只显示一次！）

**TestPyPI:**
1. 登录 https://test.pypi.org
2. 同样的步骤生成 token

### 2.2 配置 Token

创建或编辑 `~/.pypirc` 文件：

```ini
[distutils]
index-servers =
    pypi
    testpypi

[pypi]
username = __token__
password = pypi-AgEIcHlwaS5vcmcC...你的token...

[testpypi]
repository = https://test.pypi.org/legacy/
username = __token__
password = pypi-AgENdGVzdC5weXBpLm9yZwI...你的token...
```

**重要：** 设置文件权限
```bash
chmod 600 ~/.pypirc
```

## 3. 安装构建工具

```bash
pip install --upgrade build twine
```

## 4. 构建分发包

在项目根目录（包含 pyproject.toml 的目录）运行：

```bash
# 清理旧的构建文件
rm -rf dist/ build/ *.egg-info

# 构建
python -m build
```

这会在 `dist/` 目录下生成两个文件：
- `mboxlabs_mailbox-0.1.0-py3-none-any.whl` (wheel 格式)
- `mboxlabs-mailbox-0.1.0.tar.gz` (源码分发)

## 5. 检查分发包

```bash
twine check dist/*
```

确保没有错误或警告。

## 6. 上传到 TestPyPI（推荐先测试）

```bash
twine upload --repository testpypi dist/*
```

或者使用完整URL：
```bash
twine upload --repository-url https://test.pypi.org/legacy/ dist/*
```

### 6.1 测试安装

```bash
pip install --index-url https://test.pypi.org/simple/ --no-deps mboxlabs-mailbox
```

## 7. 上传到 PyPI（正式发布）

确认测试无误后：

```bash
twine upload dist/*
```

## 8. 验证发布

访问：https://pypi.org/project/mboxlabs-mailbox/

安装测试：
```bash
pip install mboxlabs-mailbox
```

## 9. 自动化发布（可选）

### 9.1 使用 GitHub Actions

创建 `.github/workflows/publish.yml`:

```yaml
name: Publish to PyPI

on:
  release:
    types: [published]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v3

    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.x'

    - name: Install dependencies
      run: |
        python -m pip install --upgrade pip
        pip install build twine

    - name: Build package
      run: python -m build

    - name: Publish to PyPI
      env:
        TWINE_USERNAME: __token__
        TWINE_PASSWORD: ${{ secrets.PYPI_API_TOKEN }}
      run: twine upload dist/*
```

### 9.2 配置 GitHub Secrets

1. 在 GitHub 仓库设置中
2. Settings → Secrets and variables → Actions
3. 添加 secret: `PYPI_API_TOKEN`（值为你的 PyPI API token）

## 10. 版本管理

### 更新版本号

编辑 `pyproject.toml`:
```toml
version = "0.2.0"  # 更新版本号
```

### 语义化版本规则

- **MAJOR.MINOR.PATCH** (例如: 1.2.3)
- **MAJOR**: 不兼容的 API 变更
- **MINOR**: 向后兼容的功能新增
- **PATCH**: 向后兼容的问题修正

## 11. 常见问题

### Q: 包名已存在
A: PyPI 包名是全局唯一的，需要选择其他名称

### Q: 上传失败 - 403 Forbidden
A: 检查 API token 是否正确，权限是否足够

### Q: 版本号已存在
A: PyPI 不允许覆盖已发布的版本，需要增加版本号

### Q: 如何删除已发布的包？
A: 可以在 PyPI 网站上删除特定版本，但不推荐（会影响依赖）

## 12. 最佳实践

1. **先在 TestPyPI 测试**
2. **使用 API Token 而不是密码**
3. **版本号遵循语义化版本**
4. **每次发布前运行测试**
5. **保持 README 和文档更新**
6. **添加 CHANGELOG.md 记录变更**
7. **使用 GitHub Releases 管理版本**

## 13. 快速发布脚本

创建 `scripts/publish.sh`:

```bash
#!/bin/bash
set -e

echo "🧹 Cleaning old builds..."
rm -rf dist/ build/ *.egg-info

echo "🔨 Building package..."
python -m build

echo "✅ Checking package..."
twine check dist/*

echo "📤 Uploading to TestPyPI..."
twine upload --repository testpypi dist/*

echo "✨ Done! Test installation with:"
echo "pip install --index-url https://test.pypi.org/simple/ --no-deps mboxlabs-mailbox"
```

使用：
```bash
chmod +x scripts/publish.sh
./scripts/publish.sh
```

## 14. 发布检查清单

- [ ] 更新版本号 (pyproject.toml)
- [ ] 更新 CHANGELOG.md
- [ ] 运行所有测试
- [ ] 更新文档
- [ ] 清理旧构建文件
- [ ] 构建新包
- [ ] 检查包内容
- [ ] 上传到 TestPyPI
- [ ] 测试安装
- [ ] 上传到 PyPI
- [ ] 创建 GitHub Release
- [ ] 验证安装

---

**首次发布建议流程：**

1. 在 TestPyPI 注册并测试
2. 在 PyPI 注册
3. 配置 API tokens
4. 构建并上传到 TestPyPI
5. 测试安装
6. 上传到 PyPI
7. 庆祝！🎉
