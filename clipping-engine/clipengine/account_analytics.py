"""Low-frequency read-only account statistics. Never persist platform credentials or API bodies."""
import time

import httpx


def collect(db, youtube, tokens, version):
    now = time.time()
    with db.tx() as conn:
        old = conn.execute("SELECT value FROM meta WHERE key='analytics.next_refresh'").fetchone()
        if old and float(old[0]) > now:
            return
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('analytics.next_refresh',?)", (str(now + 86400),))
    accounts = db.query("SELECT * FROM social_accounts WHERE identity_verified_at IS NOT NULL "
                        "AND platform IN ('youtube','instagram','facebook') ORDER BY id")
    limited = set()
    with httpx.Client(timeout=12) as client:
        for account in accounts:
            platform = account["platform"]
            audience = views = None
            reason = None
            try:
                if platform in limited:
                    raise ValueError("Platform rate limit")
                identity = str(account["verified_platform_id"])
                if platform == "youtube":
                    token = youtube.oauth(account["brand"]).access_token()
                    url = "https://www.googleapis.com/youtube/v3/channels"
                    params = {"part": "statistics", "id": identity}
                else:
                    token = (tokens.get(f"{platform}:{account['brand']}") or {}).get("access_token")
                    url = f"https://graph.facebook.com/{version}/{identity}"
                    field = "followers_count"
                    params = {"fields": f"id,{field}"}
                if not token:
                    raise ValueError("Authorisation unavailable")
                response = client.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
                if response.status_code == 429 or (response.status_code == 403 and platform == "youtube"):
                    limited.add(platform)
                response.raise_for_status()
                body = response.json()
                if platform == "youtube":
                    row = next(r for r in body["items"] if r["id"] == identity)
                    stats = row["statistics"]
                    views = int(stats["viewCount"]) if "viewCount" in stats else None
                    if not stats.get("hiddenSubscriberCount") and "subscriberCount" in stats:
                        audience = int(stats["subscriberCount"])
                elif str(body.get("id")) == identity and body.get(field) is not None:
                    audience = int(body[field])
                if audience is None and views is None:
                    reason = "Metric unavailable"
            except httpx.HTTPStatusError as err:
                reason = f"Platform returned HTTP {err.response.status_code}"
            except Exception:
                reason = "Account statistics unavailable"
            db.insert("account_snapshots", account_id=account["id"], collected_at=now,
                      audience=audience, views=views, error=reason)
            if reason is None:
                try:
                    collect_posts(db, client, account, token, version, now)
                except httpx.HTTPStatusError:
                    limited.add(platform)


def published_posts(db, account):
    # Older attempts lack job_id. Only accept a unique recorded destination for the clip/platform.
    return db.query(
        """WITH routes AS (
             SELECT DISTINCT j.clip_id, a.platform, a.id AS account_id
             FROM jobs j JOIN social_accounts a ON a.id=json_extract(
                 CASE WHEN json_valid(j.payload_json) THEN j.payload_json ELSE '{}' END,'$.account_id')
             WHERE j.kind='deliver_clip'
           )
           SELECT p.id,p.clip_id,p.remote_id FROM publish_attempts p JOIN routes r
             ON r.clip_id=p.clip_id AND r.platform=p.platform
           WHERE r.account_id=? AND p.status='published' AND p.remote_id IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM routes other WHERE other.clip_id=r.clip_id
                             AND other.platform=r.platform AND other.account_id<>r.account_id)
           ORDER BY p.id DESC LIMIT 20""", (account["id"],))


def collect_posts(db, client, account, token, version, now):
    platform = account["platform"]
    for post in published_posts(db, account):
        remote = post["remote_id"]
        metrics = {}
        error = None
        try:
            if platform == "youtube":
                url = "https://www.googleapis.com/youtube/v3/videos"
                params = {"part": "snippet,statistics", "id": remote}
            else:
                url = f"https://graph.facebook.com/{version}/{remote}"
                params = {"fields": "id,like_count,comments_count" if platform == "instagram"
                          else "id,comments.limit(0).summary(true),likes.limit(0).summary(true)"}
            response = client.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
            data = response.json()
            if platform == "youtube":
                item = next(i for i in data["items"] if i["id"] == remote and
                            i["snippet"]["channelId"] == account["verified_platform_id"])
                stats = item["statistics"]
                metrics = {"views": stats.get("viewCount"), "likes": stats.get("likeCount"),
                           "comments": stats.get("commentCount")}
            elif data.get("id") == remote:
                if platform == "instagram":
                    metrics = {"likes": data.get("like_count"), "comments": data.get("comments_count")}
                else:
                    metrics = {key: data.get(key, {}).get("summary", {}).get("total_count")
                               for key in ("likes", "comments")}
        except httpx.HTTPStatusError as exc:
            error = f"Platform returned HTTP {exc.response.status_code}"
            if exc.response.status_code in (403, 429):
                raise
        except (KeyError, StopIteration, ValueError, httpx.RequestError):
            error = "Post statistics unavailable"
        db.insert("post_snapshots", account_id=account["id"], attempt_id=post["id"], collected_at=now,
                  error=error, **metrics)
