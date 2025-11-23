#!/bin/bash
set -e

echo "🧹 Cleaning old builds..."
rm -rf dist/ build/ *.egg-info

echo "🔨 Building package..."
python -m build

echo "✅ Checking package..."
twine check dist/*

read -p "Upload to TestPyPI? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]
then
    echo "📤 Uploading to TestPyPI..."
    twine upload --repository testpypi dist/*
    echo ""
    echo "✨ Test installation with:"
    echo "pip install --index-url https://test.pypi.org/simple/ --no-deps mboxlabs-mailbox"
fi

echo ""
read -p "Upload to PyPI? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]
then
    echo "📤 Uploading to PyPI..."
    twine upload dist/*
    echo ""
    echo "🎉 Published! Install with:"
    echo "pip install mboxlabs-mailbox"
fi
