#!/bin/bash

# 定义变量
APP_NAME="pdf-distributor"
PORT=10031

# 颜色输出
GREEN='\033[0;32m'
NC='\033[0m' # No Color

echo -e "${GREEN}>>> 开始部署流程: ${APP_NAME}${NC}"

# 1. 检查 .env 是否存在
if [ ! -f .env ]; then
    echo "❌ 错误: 未找到 .env 文件。请先创建并配置环境变量。"
    exit 1
fi

# 2. 确保 token 文件存在（否则Docker会将其作为目录挂载）
if [ ! -f baidu_token.json ]; then
    echo "⚠️  未找到 baidu_token.json，创建空文件以持久化授权信息..."
    touch baidu_token.json
    echo "{}" > baidu_token.json
fi

# 3. 拉取最新代码
echo -e "${GREEN}>>> 拉取 Git 最新代码...${NC}"
git pull

# 4. 构建 Docker 镜像
echo -e "${GREEN}>>> 构建 Docker 镜像...${NC}"
docker build -t ${APP_NAME}:latest .

# 5. 停止并删除旧容器
if [ "$(docker ps -q -f name=${APP_NAME})" ]; then
    echo -e "${GREEN}>>> 停止旧容器...${NC}"
    docker stop ${APP_NAME}
fi

if [ "$(docker ps -aq -f name=${APP_NAME})" ]; then
    echo -e "${GREEN}>>> 删除旧容器...${NC}"
    docker rm ${APP_NAME}
fi

# 6. 启动新容器
# -v $(pwd)/baidu_token.json:/app/baidu_token.json 确保授权不过期
# -v $(pwd)/WM.Feishu.png:/app/WM.Feishu.png 挂载水印文件（如果有）
echo -e "${GREEN}>>> 启动新容器 (宿主机端口: ${PORT} -> 容器端口: 8501)...${NC}"
docker run -d \
  --name ${APP_NAME} \
  --restart unless-stopped \
  -p ${PORT}:${PORT} \
  --env-file .env \
  -v "$(pwd)/baidu_token.json":/app/baidu_token.json \
  ${APP_NAME}:latest

echo -e "${GREEN}>>> 部署完成!${NC}"
echo -e "访问地址: http://YOUR_VPS_IP:${PORT}"