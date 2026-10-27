"""群聊热点词：中文 2-gram + 英文单词统计，够轻量（不引第三方分词库）。"""
from __future__ import annotations

import re
from collections import Counter

# 高频虚词/机器人自己的词，统计时过滤掉，避免词云全是"什么/这个/大肥鱼"
STOPWORDS = {
    "我们", "你们", "他们", "她们", "什么", "怎么", "这个", "那个", "就是", "不是",
    "可以", "没有", "现在", "今天", "昨天", "明天", "一个", "还是", "知道", "时候",
    "这样", "感觉", "真的", "已经", "因为", "所以", "但是", "然后", "如果", "可能",
    "应该", "自己", "一起", "出来", "起来", "东西", "有点", "多少", "多久", "哪里",
    "为什么", "怎么样", "是不是", "有没有", "哈哈", "哈哈哈", "哈哈哈哈",
    "大肥鱼", "肥鱼", "鲸鱼", "蓝色", "表情", "图片", "消息", "群里", "群友",
    "说话", "回复", "一条", "一下", "一直", "这些", "那些", "时候", "一样",
    "情包", "你的", "我的", "他的", "这只", "那张", "这种", "那个",
    "字为", "空消", "无文", "是什", "面无", "欢你", "不能", "什么",
    # 脏话不进成绩单
    "傻逼", "妈的", "卧槽", "操你", "草泥", "废物", "滚蛋", "脑残",
}

_WORD_RE = re.compile(r"[a-zA-Z]{3,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")


def build_word_cloud(
    texts: list[str], top: int = 30, min_count: int = 2
) -> list[dict]:
    """统计高频词；返回 [{word, count}]，按出现次数降序。"""
    counts: Counter[str] = Counter()
    for text in texts or []:
        body = str(text or "")
        if not body:
            continue
        for part in _CJK_RE.findall(body):
            if len(part) < 2:
                continue
            for index in range(len(part) - 1):
                gram = part[index:index + 2]
                if gram in STOPWORDS:
                    continue
                counts[gram] += 1
        for word in _WORD_RE.findall(body):
            counts[word.lower()] += 1
    ranked = [
        {"word": word, "count": count}
        for word, count in counts.most_common(max(1, top) * 3)
        if count >= max(1, min_count)
    ]
    return ranked[: max(1, top)]
