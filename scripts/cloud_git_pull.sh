#!/bin/bash
# 云端：从 Git 仓库拉取最新代码，更新到 qq-bot 容器并重启
#
# 首次部署（只做一次，需要仓库地址 + token）：
#   sudo mkdir -p /opt/qq-deepseek-bot
#   sudo git clone https://oauth2:TOKEN@gitee.com/你的用户名/qq-deepseek-bot.git /opt/qq-deepseek-bot/code
#   # 以后每次执行本脚本即可
#
# 用法：
#   bash /opt/qq-deepseek-bot/code/scripts/cloud_git_pull.sh
#
# 也可以加定时拉取（每 5 分钟检查一次，有更新就自动重启）：
#   crontab -e
#   */5 * * * * bash /opt/qq-deepseek-bot/code/scripts/cloud_git_pull.sh >> /home/ubuntu/git_pull.log 2>&1

set -e

REPO_DIR="${REPO_DIR:-/opt/qq-deepseek-bot/code}"
CONTAINER="${CONTAINER:-qq-bot}"
BRANCH="${BRANCH:-master}"

cd "$REPO_DIR"

echo "==> 拉取最新代码（$BRANCH）"
BEFORE=$(git rev-parse HEAD)
git fetch origin "$BRANCH"
git reset --hard "origin/$BRANCH"
AFTER=$(git rev-parse HEAD)

if [ "$BEFORE" = "$AFTER" ]; then
    echo "==> 代码没有变化（$AFTER），跳过重启"
    exit 0
fi

echo "==> 代码已更新：$BEFORE -> $AFTER"

echo "==> 同步到容器 $CONTAINER"
sudo docker cp "$REPO_DIR/src/." "$CONTAINER:/app/src/"

echo "==> 重启 bot 容器"
sudo docker restart "$CONTAINER"
sleep 6

echo "==> 重启后日志"
sudo docker logs "$CONTAINER" 2>&1 | tail -20
echo "==> 完成"
