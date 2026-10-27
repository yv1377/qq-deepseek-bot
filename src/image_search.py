"""Wikimedia Commons 搜图并收藏，记录来源和授权元数据。"""
import json
from urllib.parse import urlsplit
import httpx


async def search_images(query: str, group_id: int, gallery) -> str:
    query = query.strip()[:150]
    if not query:
        return "搜索词不能为空"
    saved = []
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False,
                                     headers={"User-Agent": "QQFriendGallery/1.0"}) as client:
            response = await client.get("https://commons.wikimedia.org/w/api.php", params={
                "action": "query", "format": "json", "generator": "search",
                "gsrsearch": query, "gsrnamespace": 6, "gsrlimit": 5,
                "prop": "imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": 640})
            response.raise_for_status()
            for page in response.json().get("query", {}).get("pages", {}).values():
                infos = page.get("imageinfo") or []
                if not infos:
                    continue
                info = infos[0]
                url = info.get("thumburl") or info.get("url") or ""
                parsed = urlsplit(url)
                if parsed.scheme != "https" or parsed.hostname != "upload.wikimedia.org":
                    continue
                data = bytearray()
                async with client.stream("GET", url) as image:
                    image.raise_for_status()
                    async for block in image.aiter_bytes():
                        data.extend(block)
                        if len(data) > gallery.cfg.max_bytes:
                            break
                origin = info.get("descriptionurl") or ""
                path = await gallery.save(group_id, bytes(data), source="search",
                                          context=query, origin=origin)
                if path:
                    meta = gallery.metadata(path)
                    meta["tags"] = query
                    meta["attribution"] = info.get("extmetadata", {})
                    path.with_suffix(path.suffix + ".json").write_text(
                        json.dumps(meta, ensure_ascii=False), encoding="utf-8")
                    saved.append(f"{page.get('title', '')} 来源：{origin}")
                if len(saved) >= 3:
                    break
    except (httpx.HTTPError, ValueError, OSError):
        return "图片搜索暂时不可用；" + ("已保存：" + "；".join(saved) if saved else "没有保存图片。")
    return "已收藏到当前群图库：" + "；".join(saved) if saved else "未找到可收藏图片，或图库已满。"
