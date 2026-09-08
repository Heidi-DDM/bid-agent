# R004/F004：公告源注册表（manual_trigger/检索 的源清单）
# 单一事实源：docs/合规数据源清单.md §2（引用 schema §1.4 平台注册表）+ 京津冀招投标平台对照表-20260819。
#
# 【2026-09-08 接入决策（用户确认，越红线需同步合规清单）】
#   将《京津冀招投标平台对照表》24 家平台全量登记进 SOURCES。
#   每家带 `collectable` + `compliance_status` 标识：
#     - collectable=True + compliance_status=verified → 已实测可自动抓列表页（当前仅惠招标）
#     - collectable=False + compliance_status=pending/contract → 登记但尚未实测到可解析列表页，
#       人工执行/后续实测后置 compliance_status=verified。搜索仍会返回该源（count=0/error），
#       但不会在未验证结构的情况下声称可采集成功。
#   ⚠️ 合规红线仍在运行时强制生效（compliance.py）：robots 预检留痕、限频、透明/合理UA、
#      manual_trigger collect_mode、不绕过技术保护、不抓登录/付费内容。本表登记 ≠ 自动化全采。
#
# 启用状态（compliance_status）：verified(已实测可解析) / pending(登记待实测) / blocked(需人工)
# collectable（是否作为“可采集公告源”被前端后端扫描）：
#   True  → 参与检索；False → 只登记不参与自动抓（如住建部四库一平台是资质库非招标源）
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 源对 region 搜索条件的覆盖口径：返回 True 表示该源可能含目标地区公告。
# 平台自身即为河北域平台时，region 为 None 或含"河北"视为覆盖。
_DEFAULT_REGION = "河北省"

# 河北省内行政区（省域平台下辖口径）：省级平台覆盖全省，省内市级检索不跳过；
# 仍不做逐条公告地区推断（列表页不标注地区 → 候选 region=null 待补，详情回源确认）。
_HEBEI_SUBREGIONS = frozenset({
    "石家庄市", "唐山市", "秦皇岛市", "邯郸市", "邢台市", "保定市",
    "张家口市", "承德市", "沧州市", "廊坊市", "衡水市",
    "雄安新区", "定州市", "辛集市",
})


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    name: str                    # 展示名（对齐清单）
    base_url: str
    level: str                   # L0/L1/L2/L3（schema §1.4 分级）: 合规清单 §1
    list_path: str               # 公告/检索列表页路径（第 1 页）；待实测平台可留 "/" 首页
    category: str                # 列表分区事实（公告原文/平台分区）
    region_scope: str = _DEFAULT_REGION
    admin_subregions: frozenset[str] = frozenset()  # scope 下辖行政区（省级平台填市级清单）
    search_param: str | None = None  # 站内检索 query 参数名；None=无检索页，回默认列表页
    page_param: str | None = None    # 分页参数；"path_page" 表示 /2.html 路径分页
    max_pages: int = 1               # 源已探测/允许的页数，COLLECTION_POLICY.max_pages 为硬上限
    date_param: str | None = None    # 日期过滤参数；None=不向平台提交日期过滤
    collectable: bool = False        # 是否作为可采集公告源（前端 / 后端批量检索参与）
    compliance_status: str = "pending"  # verified(已实测可解析) / pending(待实测) / blocked(需人工)
    legality_note: str = "待实测/法务确认；无论状态，实时机器人留痕不阻断、限速、短 URL、不验证码强制生效"

    @property
    def list_url(self) -> str:
        return f"{self.base_url}{self.list_path}"

    def page_url(self, page: int, *, list_url: str | None = None) -> str:
        """构造列表页 URL；page 从 1 开始，保持已有检索 query 参数。
        惠招标 = trade.html + /2.html 路径分页；其他用 page_param query。
        """
        if page < 1:
            raise ValueError("page must be >= 1")
        current = list_url or self.list_url
        if page == 1 or not self.page_param:
            return current
        parsed = urlsplit(current)
        if self.page_param in ("path", "path_page"):
            path = parsed.path
            if path.endswith(".html"):
                path = f"{path.rsplit('/', 1)[0]}/{page}.html"
            else:
                path = f"{path.rstrip('/')}/{page}.html"
            return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))
        query = parse_qsl(parsed.query, keep_blank_values=True)
        query = [(key, value) for key, value in query if key != self.page_param]
        query.append((self.page_param, str(page)))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                           urlencode(query), parsed.fragment))

    def covers_region(self, region: str | None) -> bool:
        """该源是否覆盖给定 region。不做逐条公告地区推断。"""
        if not region:
            return True
        if region == self.region_scope or region in self.admin_subregions:
            return True
        return self.region_scope in region or region in self.region_scope


# ── 搜索筛选口径（地区/种类/规模三条件） ────────────────────────────────
SEARCH_CATEGORY_OPTIONS: tuple[str, ...] = (
    "全部种类",
    "房屋建筑", "市政公用", "公路工程", "水利水电", "通信工程", "机电工程",
)

_CATEGORY_TITLE_WORDS: dict[str, tuple[str, ...]] = {
    "房屋建筑": ("房屋建筑", "房建", "住宅", "宿舍", "教学楼"),
    "市政公用": ("市政", "道路工程", "管网", "排水", "供水"),
    "公路工程": ("公路", "高速", "桥梁", "隧道"),
    "水利水电": ("水利", "水电", "水库", "河道", "堤防"),
    "通信工程": ("通信", "光纤", "基站"),
    "机电工程": ("机电", "电气", "设备安装"),
}


def category_matches(category_value: str | None, title: str | None) -> bool:
    """种类筛选命中判断（纯函数，可注入测试）。"""
    if not category_value or category_value == "全部种类":
        return True
    title_norm = _normalize_keyword(title or "")
    return any(word in title_norm for word in _CATEGORY_TITLE_WORDS.get(category_value, (category_value,)))


def _normalize_keyword(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def keyword_matches(keyword: str | None, title: str | None) -> bool:
    """确定性关键词命中：归一后多词 AND（词间空白即分隔）；空关键词恒命中。"""
    kw = _normalize_keyword(keyword)
    if not kw:
        return True
    title_norm = _normalize_keyword(title or "")
    return all(word in title_norm for word in kw.split(" ") if word)


# ── 已注册源（2026-07-08 全量接入，含惠招标真源 + 其余平台登记） ──────────
def _prov_scope(province: str) -> str:
    return f"{province}省"


SOURCES: dict[str, SourceSpec] = {
    # ------ ：原学者（河北交投）4 分区（已实测可解析，L1） ------
    "hebtig": SourceSpec(
        source_id="hebtig", name="惠招标（河北交投）工程类",
        base_url="https://ebidding.hebtig.com", level="L1",
        list_path="/jyxx/001001/001001001/trade.html", category="招标专区-工程类",
        region_scope="河北省", admin_subregions=_HEBEI_SUBREGIONS,
        search_param="keyword", page_param="path_page", max_pages=6,
        collectable=True, compliance_status="verified",
    ),
    "hebtig_service": SourceSpec(
        source_id="hebtig_service", name="惠招标（河北交投）服务类",
        base_url="https://ebidding.hebtig.com", level="L1",
        list_path="/jyxx/001001/001001003/trade.html", category="招标专区-服务类",
        admin_subregions=_HEBEI_SUBREGIONS,
        search_param="keyword", page_param="path_page", max_pages=6,
        collectable=True, compliance_status="verified",
    ),
    "hebtig_goods": SourceSpec(
        source_id="hebtig_goods", name="惠招标（河北交投）货物类",
        base_url="https://ebidding.hebtig.com", level="L1",
        list_path="/jyxx/001001/001001004/trade.html", category="招标专区-货物类",
        admin_subregions=_HEBEI_SUBREGIONS,
        search_param="keyword", page_param="path_page", max_pages=6,
        collectable=True, compliance_status="verified",
    ),
    "hebtig_nzb": SourceSpec(
        source_id="hebtig_nzb", name="惠招标（河北交投）非招标专区",
        base_url="https://ebidding.hebtig.com", level="L1",
        list_path="/jyxx/001002/001002001/trade.html", category="非招标专区",
        admin_subregions=_HEBEI_SUBREGIONS,
        search_param="keyword", page_param="path_page", max_pages=6,
        collectable=True, compliance_status="verified",
    ),


    # ---- 国家级公开资源平台（2026-09-08 探测：ccgp 列表可达；ggzy 有 TLS/路径问题） ----
    "ggzy": SourceSpec(
        source_id="ggzy", name="全国公共资源交易平台", level="L1",
        base_url="https://www.ggzy.gov.cn",
        list_path="/information/lis.html",  # 待实测（/information/lis.html 404，TLS 握手偶发超时）
        category="招标公告", region_scope="全国", collectable=True,
        compliance_status="pending",  # 路径/通断待实测，暂标记可采但可能抓不到
    ),
    "ccgp": SourceSpec(
        source_id="ccgp", name="中国政府采购网", level="L1",
        base_url="http://www.ccgp.gov.cn", list_path="/cggg/dfgg/",
        category="招标公告", region_scope="全国", collectable=True,
        compliance_status="verified",  # 2026-09-08 探测 /cggg/dfgg/ 200
        search_param="gp", page_param="pageNo",
    ),
    "cebpubservice": SourceSpec(
        source_id="cebpubservice", name="中国招标投标公共服务平台", level="L1",
        base_url="https://www.cebpubservice.com", list_path="/",
        category="招标公告", region_scope="全国", collectable=True,
        compliance_status="pending",  # 首页 200，但公告列表入口需再探
    ),

    # —— 省级（L1，公告免费；2026-09-08 首页均 200 可达） ----
        "ggzyfw-beijing": SourceSpec(
            source_id="ggzyfw-beijing", name="北京公共资源交易服务平台", level="L1",
            base_url="https://ggzyfw.beijing.gov.cn", list_path="/",
            category="招标公告", region_scope="北京市", collectable=True,
            compliance_status="pending",  # 首页 200；公告列表入口待细探
        ),
        "ccgp-beijing": SourceSpec(
            source_id="ccgp-beijing", name="北京市政府采购网", level="L1",
            base_url="http://www.ccgp-beijing.gov.cn",
            list_path="/xxgg/A002004index_1.htm",  # 市本级招标公告入口（xlsx 实测样例）
            category="招标公告", region_scope="北京市", collectable=True,
            compliance_status="pending",  # 首页 200；公告列表页入口待实测
        ),
        "ccgp-tianjin": SourceSpec(
            source_id="ccgp-tianjin", name="天津市政府采购网", level="L1",
            base_url="http://www.ccgp-tianjin.gov.cn", list_path="/",
            category="招标公告", region_scope="天津市", collectable=True,
            compliance_status="pending",  # 首页 200；公告列表入口待实测
        ),
        "ggzy-tianjin": SourceSpec(
            source_id="ggzy-tianjin", name="天津市公共资源交易平台", level="L1",
            base_url="https://ggzy.zwfwb.tj.gov.cn", list_path="/p54/jyxxgcjs.html",
            category="招标公告", region_scope="天津市", collectable=True,
            compliance_status="pending",  # 首页 200；工程列表入口待实测（样例 p54/jyxxgcjs.html）
        ),
        "ccgp-hebei-web": SourceSpec(
            source_id="ccgp-hebei-web", name="河北省政府采购网", level="L1",
            base_url="http://www.ccgp-hebei.gov.cn", list_path="/province/cggg/zbgg/",
            category="招标公告", region_scope="河北省", admin_subregions=_HEBEI_SUBREGIONS,
            collectable=True, compliance_status="verified",
            # 2026-09-08 探测 /province/cggg/zbgg/ 200，列表页含公告链接
            # 全站 robots Disallow: / （ADR-003 预检留痕不阻断，已在 compliance 生效）
        ),
        "szj-hebei": SourceSpec(
            source_id="szj-hebei", name="河北省公共资源交易服务平台", level="L1",
            base_url="https://szj.hebei.gov.cn", list_path="/hbggfwpt/",
            category="招标公告", region_scope="河北省", admin_subregions=_HEBEI_SUBREGIONS,
            collectable=True, compliance_status="verified",
            # 2026-09-08 探测 /hbggfwpt/ 200，首页可提取各地市/分区入口
        ),
        "zhjy-bcactc": SourceSpec(
            source_id="zhjy-bcactc", name="北京建设工程交易系统", level="L1",
            base_url="https://zhjy.bcactc.com", list_path="/",
            category="房建/市政", region_scope="北京市", collectable=True,
            compliance_status="pending",  # 首页 200；公告列表入口待实测
        ),

    # —— 河北 11 市（L2 地市自建，逐个合规评估后启用；UBI source）——
    "sjzsggzy": SourceSpec(
        source_id="sjzsggzy", name="石家庄市公共资源交易中心", level="L2",
        base_url="http://www.sjzsggzyjyzx.org.cn", list_path="/",
        category="招标公告", region_scope="石家庄市", collectable=True,
        compliance_status="pending",  # 2026-09-08 首页 200；公告列表入口待实测
    ),
    "tsggzy": SourceSpec(
        source_id="tsggzy", name="唐山市公共资源交易中心", level="L2",
        base_url="http://ggzyjy.xzspj.tangshan.gov.cn", list_path="/",
        category="招标公告", region_scope="唐山市", collectable=True,
        compliance_status="pending",  # 2026-09-08 首页 200；公告列表入口待实测
    ),
    "hdggzy": SourceSpec(
        source_id="hdggzy", name="邯郸市公共资源交易中心", level="L2",
        base_url="https://ggzy.hd.gov.cn", list_path="/",
        category="招标公告", region_scope="邯郸市", collectable=True,
        compliance_status="pending",  # 2026-09-08 首页 200；公告列表入口待实测
    ),
    "qhdggzy": SourceSpec(
        source_id="qhdggzy", name="秦皇岛市公共资源交易中心", level="L2",
        base_url="http://www.qhdggzy.cn/qhdggzy/", list_path="/",
        category="招标公告", region_scope="秦皇岛市", collectable=False,
        compliance_status="blocked",  # 2026-09-08 探测 404（入口路径待确认，暂不采）
    ),
    "cdggzy": SourceSpec(
        source_id="cdggzy", name="承德市公共资源交易中心", level="L2",
        base_url="http://szj.chengde.gov.cn", list_path="/cdsggzy/",
        category="招标公告", region_scope="承德市", collectable=True,
        compliance_status="pending",  # 2026-09-08 首页 200；公告列表入口待实测
    ),
    "hsggzy": SourceSpec(
        source_id="hsggzy", name="衡水市公共资源交易中心", level="L2",
        base_url="http://hsggzy.hengshui.gov.cn", list_path="/",
        category="招标公告", region_scope="衡水市", collectable=True,
        compliance_status="pending",  # 2026-09-08 首页 200；公告列表入口待实测
    ),
    "cangzhou": SourceSpec(
        source_id="cangzhou", name="沧州市公共资源交易中心", level="L2",
        base_url="https://xzsp.cangzhou.gov.cn", list_path="/xzsp/add100115/",
        category="招标公告", region_scope="沧州市", collectable=True,
        compliance_status="pending",  # 2026-09-08 首页 200；公告列表入口待实测
    ),
    "xingtai": SourceSpec(
        source_id="xingtai", name="邢台市公共资源交易中心", level="L2",
        base_url="http://60.6.198.121:8888", list_path="/sszt-zyjyPortal/",
        category="招标公告", region_scope="邢台市", collectable=False,
        compliance_status="blocked",  # 2026-09-08 探测 404（IP 直连，当前不可达，JS 门户）
    ),


    # —— L0 第三方平台（需合同/API，仅登记，不接入 txt 采集） ——
    "qianlima": SourceSpec(
        source_id="qianlima", name="千里马招标网", level="L0",
        base_url="https://www.qianlima.com", list_path="/",
        category="招标公告", region_scope="全国", collectable=False,
        compliance_status="blocked",
    ),
    "jianyu360": SourceSpec(
        source_id="jianyu360", name="剑鱼标讯", level="L0",
        base_url="https://www.jianyu360.com", list_path="/",
        category="招标公告", region_scope="全国", collectable=False,
        compliance_status="blocked",
    ),
    "bidcenter": SourceSpec(
        source_id="bidcenter", name="采招网", level="L0",
        base_url="https://www.bidcenter.com.cn", list_path="/",
        category="招标公告", region_scope="全国", collectable=False,
        compliance_status="blocked",
    ),
    "bidizhaobiao": SourceSpec(
        source_id="bidizhaobiao", name="比地招标", level="L0",
        base_url="https://www.bidizhaobiao.cn", list_path="/",
        category="招标公告", region_scope="全国", collectable=False,
        compliance_status="blocked",
    ),
}


def get(source_id: str) -> SourceSpec | None:
    return SOURCES.get(source_id)


def validate_sources(sources: list[str] | None) -> tuple[list[str], list[str]]:
    """校验请求中的 sources：返回 (有效, 未知)，并按首次出现顺序去重。
    空/None → 全部已注册源（全量对接清单）。L0 已登记但 collectable=False 的，
    仅登记，不参与批量抓取；但 validate 仍视为有效（"已注册"），
    采集侧按 collectable 过滤是否可抓取。
    """
    if not sources:
        return list(SOURCES), []
    valid, unknown = [], []
    seen_valid: set[str] = set()
    seen_unknown: set[str] = set()
    for sid in sources:
        if sid in SOURCES:
            if sid not in seen_valid:
                valid.append(sid)
                seen_valid.add(sid)
        elif sid not in seen_unknown:
            unknown.append(sid)
            seen_unknown.add(sid)
    return valid, unknown