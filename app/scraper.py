"""公告采集：抓取同站分页列表与全部详情页，失败逐条记录。

跳转策略（列表与详情一致）：
- 只跟随与“原公告站”（请求入口列表页）同源的跳转，跨站跳转直接拒绝并记录；
- 站内跳转后，页面内的相对分页/详情链接按最终页面地址解析；
- 分页循环与详情重复均按最终地址识别；
- 跨站跳转或跳转链失败（循环/超限/缺 Location）记录问题地址，
  由调度层标记 undetermined，绝不把未核实的路段报为畅通。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx

from .parser import ClosureAnnouncement, Listing, normalize_url, parse_detail, parse_listing

MAX_PAGES = 50  # 防止分页环路导致的无限抓取
MAX_REDIRECTS = 10  # 单地址跳转链长度上限，防止跳转环路耗尽资源
REQUEST_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
RETRIES = 1  # 失败后再尝试 1 次（共 2 次请求）


class RedirectChainError(Exception):
    """跳转链确定性错误（跨站/循环/超限/缺 Location）：不重试，直接记录。"""


@dataclass
class FetchedPage:
    """一次抓取的结果：内容与其最终页面地址（跳转链终点）。"""

    final_url: str
    html: str


@dataclass
class FetchFailureRecord:
    url: str
    stage: str
    reason: str


@dataclass
class ScrapeResult:
    announcements: list[ClosureAnnouncement] = field(default_factory=list)
    failures: list[FetchFailureRecord] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def _origin(url: str) -> tuple[str, str]:
    p = urlparse(url)
    return (p.scheme.lower(), p.netloc.lower())


async def _fetch_chain(
    client: httpx.AsyncClient, url: str, root_origin: tuple[str, str]
) -> FetchedPage:
    """手动跟随跳转链：每一跳都必须与原公告站同源，返回最终地址与内容。"""
    seen: set[str] = set()
    current = normalize_url(url)
    for _ in range(MAX_REDIRECTS):
        if current in seen:
            raise RedirectChainError(f"跳转链循环: {current}")
        seen.add(current)
        resp = await client.get(current)
        if resp.is_redirect:
            target = urljoin(current, resp.headers["location"])
            if _origin(target) != root_origin:
                raise RedirectChainError(f"跨站跳转被拒绝: {current} -> {target}")
            current = normalize_url(target)
            continue
        if 300 <= resp.status_code < 400:
            # 3xx 但无 Location 头，跳转链无法继续
            raise RedirectChainError(
                f"跳转缺少 Location 头: {current} (HTTP {resp.status_code})"
            )
        resp.raise_for_status()
        return FetchedPage(final_url=current, html=resp.text)
    raise RedirectChainError(f"跳转次数超过上限({MAX_REDIRECTS}): {url}")


async def _get(
    client: httpx.AsyncClient, url: str, root_origin: tuple[str, str]
) -> FetchedPage:
    last_exc: Exception | None = None
    for _ in range(RETRIES + 1):
        try:
            return await _fetch_chain(client, url, root_origin)
        except RedirectChainError:
            raise  # 确定性错误：重试不会改变结果
        except httpx.HTTPError as exc:  # 含状态码错误、超时、连接错误
            last_exc = exc
    raise last_exc  # type: ignore[misc]


async def scrape(list_url: str) -> ScrapeResult:
    """采集入口：分页列表 -> 去重详情链接 -> 逐页解析详情。"""
    result = ScrapeResult()
    headers = {"User-Agent": "coldchain-dispatch-checker/1.0"}
    root_origin = _origin(list_url)  # 原公告站：全部跳转都不得离开该源

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT, follow_redirects=False, headers=headers
    ) as client:
        detail_urls = await _collect_listing_pages(client, list_url, root_origin, result)

        seen_final_details: set[str] = set()
        for detail_url in detail_urls:
            try:
                page = await _get(client, detail_url, root_origin)
                final_url = normalize_url(page.final_url)
                if final_url in seen_final_details:
                    continue  # 不同链接跳转到同一详情：按最终地址去重
                seen_final_details.add(final_url)
                result.announcements.append(parse_detail(page.html, final_url))
            except Exception as exc:  # 详情失败：记录并继续，绝不静默丢弃
                result.failures.append(
                    FetchFailureRecord(
                        url=detail_url, stage="detail", reason=_reason(exc)
                    )
                )

    return result


async def _collect_listing_pages(
    client: httpx.AsyncClient,
    first_url: str,
    root_origin: tuple[str, str],
    result: ScrapeResult,
) -> list[str]:
    """遍历同站分页，汇总去重后的详情链接；分页循环按最终页面地址识别。"""
    collected: list[str] = []
    seen_pages: set[str] = set()
    seen_detail_links: set[str] = set()
    url = normalize_url(first_url)

    for _ in range(MAX_PAGES):
        if url in seen_pages:
            break
        try:
            page = await _get(client, url, root_origin)
        except Exception as exc:
            result.failures.append(
                FetchFailureRecord(url=url, stage="list", reason=_reason(exc))
            )
            break

        final_url = normalize_url(page.final_url)
        if final_url in seen_pages:
            break  # 跳转回到已抓取的列表页：视为分页结束
        seen_pages.add(final_url)

        # 相对分页/详情链接按最终页面地址解析
        listing: Listing = parse_listing(page.html, final_url)
        for link in listing.detail_links:
            key = normalize_url(link)
            if key not in seen_detail_links:
                seen_detail_links.add(key)
                collected.append(link)

        if not listing.next_page_url:
            break
        url = normalize_url(listing.next_page_url)

    return collected


def _reason(exc: Exception) -> str:
    if isinstance(exc, RedirectChainError):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "请求超时"
    if isinstance(exc, httpx.ConnectError):
        return "连接失败"
    return f"{type(exc).__name__}: {exc}"
