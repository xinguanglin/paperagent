@echo off
chcp 65001 >nul
cd /d %~dp0

echo [1/4] 激活 conda 环境 mcp_rag...
call conda activate mcp_rag
if errorlevel 1 (
    echo [错误] 未找到 conda 或 mcp_rag 环境。请先创建：conda create -n mcp_rag python=3.11
    pause
    exit /b 1
)

echo [2/4] 安装后端依赖...
pip install -r server\requirements.txt -q

echo [3/4] 准备配置文件...
if not exist config.yaml (
    copy config.example.yaml config.yaml >nul
    echo   已生成 config.yaml，稍后请填写智谱 API Key。
)

echo [4/4] 初始化数据库（幂等，已有表不会重建）...
cd server
python -c "from app.config import load_config; from app.database import init_db, init_engine; init_db(init_engine(load_config()))"
cd ..

echo.
echo ============================================================
echo  初始化完成！
echo  1. 编辑 config.yaml：填写 zhipu_api_key（必填）与 proxy（可选）
echo  2. 双击 start.bat 启动，浏览器访问 http://localhost:8787
echo ============================================================
pause
