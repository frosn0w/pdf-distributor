# 使用官方轻量级 Python 镜像
FROM python:3.10-slim
# 设置工作目录
WORKDIR /app

# 防止 Python 生成 .pyc 文件
ENV PYTHONDONTWRITEBYTECODE=1
# 确保控制台输出不被缓冲
ENV PYTHONUNBUFFERED=1

# 安装系统依赖：PyMuPDF 需要基本的构建工具
RUN apt-get update && apt-get install -y \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 创建缓存目录并确保权限
RUN mkdir -p /app/output_cache && chmod 777 /app/output_cache

# 暴露端口与 deploy.sh 保持一致
EXPOSE 10031

# 启动命令
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=10031"]