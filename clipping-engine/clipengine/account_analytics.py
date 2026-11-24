"""Low-frequency read-only account statistics. Never persist platform credentials or API bodies."""
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx

from .cloud import GoogleReconnectionRequired


YOUTUBE_REPORT_METRICS = ("views", "likes", "comments", "shares", "subscribersGained", "subscribersLost")


def meta_report(client, platform, identity, token, version, now):
    end = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    values = {}
    for days in (7, 30):
        params = {"period": "day", "since": int((end - timedelta(days=days)).timestamp()),
                  "until": int(end.timestamp())}
        if platform == "facebook":
            params["metric"] = "page_media_view"
        else:
            params.update(metric="views,reach,total_interactions", metric_type="total_value")
        response = client.get(f"https://graph.facebook.com/{version}/{identity}/insights",
                              params=params, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        values[str(days)] = {}
        for metric in response.json().get("data", []):
            if platform == "facebook":
                daily = [row.get("value") for row in metric.get("values", [])]
                if len(daily) == days and all(isinstance(value, int) for value in daily):
                    values[str(days)]["views"] = sum(daily)
            elif isinstance(metric.get("total_value", {}).get("value"), int):
                values[str(days)][metric["name"]] = metric["total_value"]["value"]
    return {"collected_at": now, "through": (end.date() - timedelta(days=1)).isoformat(), "values": values}


def youtube_report(client, token, now):
    end = datetime.fromtimestamp(now, timezone.utc).date() - timedelta(days=1)
    response = client.get(
        "https://youtubeanalytics.googleapis.com/v2/reports",
        params={"ids": "channel==MINE", "startDate": (end - timedelta(days=29)).isoformat(),
                "endDate": end.isoformat(), "dimensions": "day", "sort": "day",
                "metrics": ",".join(YOUTUBE_REPORT_METRICS)},
        headers={"Authorization": f"Bearer {token}"},
    )
    response.raise_for_status()
    data = response.json()
    columns = [header["name"] for header in data["columnHeaders"]]
    return [dict(zip(columns, row, strict=True)) for row in data.get("rows", [])]


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
            except GoogleReconnectionRequired:
                reason = "Reconnect account"
            except httpx.HTTPStatusError as err:
                reason = ("Reconnect account" if err.response.status_code == 401 else
                          f"Platform returned HTTP {err.response.status_code}")
            except Exception:
                reason = "Account statistics unavailable"
            db.insert("account_snapshots", account_id=account["id"], collected_at=now,
                      audience=audience, views=views, error=reason)
            if platform == "youtube" and "youtube-report" not in limited and hasattr(youtube, "analytics_oauth"):
                oauth = youtube.analytics_oauth(account["brand"])
                if oauth.connected():
                    key = f"analytics.youtube.{account['id']}"
                    try:
                        report = youtube_report(client, oauth.access_token(), now)
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code == 429:
                            limited.add("youtube-report")
                        db.set_kv(f"{key}.error", f"YouTube report returned HTTP {exc.response.status_code}")
                    except Exception:
                        db.set_kv(f"{key}.error", "Could not refresh YouTube report. Check the analytics connection.")
                    else:
                        db.set_kv(key, json.dumps({"collected_at": now, "rows": report}))
                        db.set_kv(f"{key}.error", "")
            if platform in ("facebook", "instagram") and f"{platform}-report" not in limited:
                grant = tokens.get(f"{platform}-analytics:{account['brand']}") or {}
                if grant.get("access_token") and grant.get("identity") == str(account["verified_platform_id"]):
                    key = f"analytics.meta.{account['id']}"
                    try:
                        report = meta_report(client, platform, grant["identity"], grant["access_token"], version, now)
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code == 429:
                            limited.add(f"{platform}-report")
                        db.set_kv(f"{key}.error", f"Meta Insights returned HTTP {exc.response.status_code}")
                    except (httpx.RequestError, KeyError, ValueError, TypeError):
                        db.set_kv(f"{key}.error", "Could not refresh Meta Insights.")
                    else:
                        db.set_kv(key, json.dumps(report))
                        db.set_kv(f"{key}.error", "")
            if reason is None:
                try:
                    collect_posts(db, client, account, token, version, now, tokens)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 429:
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


def collect_posts(db, client, account, token, version, now, tokens=None):
    platform = account["platform"]
    grant = (tokens.get(f"{platform}-analytics:{account['brand']}") or {}) if tokens else {}
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
                params = {"fields": "id,like_count,comments_count,permalink" if platform == "instagram"
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
                    link = urlsplit(data.get("permalink") or "")
                    if link.scheme == "https" and link.netloc in ("www.instagram.com", "instagram.com"):
                        db.set_kv(f"analytics.permalink.{post['id']}", link.geturl())
                else:
                    metrics = {key: data.get(key, {}).get("summary", {}).get("total_count")
                               for key in ("likes", "comments")}
                if grant.get("identity") == str(account["verified_platform_id"]) and grant.get("access_token"):
                    edge, requested = ("insights", {"views": "views", "shares": "shares"}) if platform == "instagram" else (
                        "video_insights", {"fb_reels_total_plays": "views"})
                    try:
                        insights = client.get(f"{url}/{edge}", params={"metric": ",".join(requested)},
                                              headers={"Authorization": f"Bearer {grant['access_token']}"})
                        insights.raise_for_status()
                        for row in insights.json().get("data", []):
                            values = row.get("values") or []
                            if row.get("name") in requested and values and isinstance(values[0].get("value"), int):
                                metrics[requested[row["name"]]] = values[0]["value"]
                    except (httpx.HTTPError, ValueError, TypeError):
                        error = "Post Insights unavailable"
        except httpx.HTTPStatusError as exc:
            error = f"Platform returned HTTP {exc.response.status_code}"
            if exc.response.status_code in (403, 429):
                raise
        except (KeyError, StopIteration, ValueError, httpx.RequestError):
            error = "Post statistics unavailable"
        db.insert("post_snapshots", account_id=account["id"], attempt_id=post["id"], collected_at=now,
                  error=error, **metrics)
