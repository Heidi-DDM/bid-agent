# R004/F004：公告源注册表（manual_trigger/检索 的源清单）
# 单一事实源：docs/合规数据源清单.md §2（引用 schema §1.4 平台注册表）+ 京津冀招投标平台对照表-20260819。
#
# 【2026-09-08 接入决策（用户确认）】
#   将《京津冀招投标平台对照表》平台全量登记进 SOURCES，并对可采集源逐一 web 探测可达性。
#   每家带 `collectable` + `compliance_status` 标识：
#     - compliance_status=verified → 已实测：HTTP 200 + 列表页含真实公告条目（静态可解析）
#     - compliance_status=pending   → 首页可达但公告列表入口/分页待校准（可参与检索，可能 count=0）
#     - compliance_status=blocked   → 当前不可达 / 需合同 / JS 动态门户（不采集）
#   ⚠️ 采集红线仍在运行时强制生效（compliance.py）：仅保留 R1 不绕过平台技术保护 + 不做恶意抓取；
#      robots 预检留痕（ADR-003）、限速、UA、collect 模式全部由 COLLECTION_POLICY 配置。
#
# collectable（是否作为可采集公告源被后端扫描）：
#   True  → 参与检索；False → 只登记不参与自动抓（blocked 或需签约）
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 源对 region 搜索条件的覆盖口径（C1，2026-09-10）：
#   direct  = 检索地区与源范围精确匹配（含：省级检索覆盖其下所有地市源，不标待核实）
#   broader = 源为省/全国大范围平台，可能含检索地区公告 → 覆盖但候选地区待核实
#   none    = 检索地区与源范围无交集 → 由 service 放宽二次召回（不整源丢弃）
_DEFAULT_REGION = "河北省"

# 河北省内行政区（省域平台下辖口径）：省级平台覆盖全省，省内市级检索不跳过。
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
    level: str                   # L0/L1/L2/L3（schema §1.4 分级）
    list_path: str               # 公告/列表页路径（第 1 页；首页可达平台可留 "/"）
    category: str                # 分区/平台事实
    region_scope: str = _DEFAULT_REGION
    parent_region: str | None = None  # 地市源所属省份（如 唐山市源 → 河北省）
    admin_subregions: frozenset[str] = frozenset()  # scope 下辖行政区
    search_param: str | None = None  # 站内检索 query 参数名；None=回默认列表页
    page_param: str | None = None    # 分页参数；"path_page"= /2.html 路径分页
    max_pages: int = 1               # 源已探测最大页码，COLLECTION_POLICY.max_pages 为硬上限
    date_param: str | None = None    # 日期过滤参数；None=不提交日期过滤
    collectable: bool = False        # 是否作为可采集公告源（参与检索/自动抓）
    compliance_status: str = "pending"  # verified(已实测可解析)/pending(待校准)/blocked(不可达或需合同)
    legality_note: str = "遵 R1 不绕过技术保护；robots 预检留痕不阻断（ADR-003）；限速/UA/模式由 COLLECTION_POLICY 控制"

    @property
    def list_url(self) -> str:
        return f"{self.base_url}{self.list_path}"

    def page_url(self, page: int, *, list_url: str | None = None) -> str:
        """构造列表页 URL；page 从 1 开始。"""
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
        query = [(k, v) for k, v in query if k != self.page_param]
        query.append((self.page_param, str(page)))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                           urlencode(query), parsed.fragment))

    def region_coverage(self, region: str | None) -> str:
        """检索地区与源范围的覆盖关系（C1 新口径，2026-09-10）。

        返回 "direct" | "broader" | "none"：
        - direct：region 为空 / region == region_scope / 省级检索覆盖其下地市源
          （地市源 parent_region == 检索省名）——正常采集，不标待核实；
        - broader：源为省级平台（检索地区在其 admin_subregions 内）或全国平台，
          可能含检索地区公告——覆盖采集，但候选逐条地区待核实（列表页不标注）；
        - none：检索地区与源范围无交集（如检索唐山市、源为北京市）——由
          service 放宽二次召回，仍抓列表 + 关键词过滤，不整源丢弃。
        """
        if not region:
            return "direct"
        if region == self.region_scope:
            return "direct"
        # 省级检索覆盖其下所有地市源（地市公告当然属于本省，不标待核实）
        if self.parent_region and region == self.parent_region:
            return "direct"
        # 全国平台含各地公告 → 覆盖但逐条地区待核实
        if self.region_scope == "全国":
            return "broader"
        # 省级平台对地市检索：平台范围覆盖该市，但公告可能来自全省任何地市
        if region in self.admin_subregions:
            return "broader"
        if self.region_scope in region or region in self.region_scope:
            return "broader"
        return "none"

    def covers_region(self, region: str | None) -> bool:
        """该源是否覆盖给定 region（direct/broader 均算覆盖；none 由调用方放宽召回）。

        兼容旧口径的布尔视图；需要区分「精确覆盖 / 大范围来源待核实」的调用方
        （service 摘要、前端卡片）请改用 region_coverage。
        """
        return self.region_coverage(region) != "none"


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


# D1（2026-09-10）：标题确定性「种类」抽取——枚举顺序即优先级（EPC/总承包类标题
# 常同时含「施工/设计」，先命中先归类）。复用 announcement_prescreen._PROJECT_TYPE
# 的词根口径，扩展为 施工/EPC总承包/监理/设计/勘察/货物/服务 七类；纯子串命中，
# 不推断；不命中 → None（种类待详情页确认）。provenance 恒为「标题」。
_TITLE_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("EPC总承包", ("EPC", "工程总承包", "设计施工总承包", "设计采购施工")),
    ("施工", ("施工",)),
    ("监理", ("监理",)),
    ("勘察", ("勘察",)),
    ("设计", ("设计",)),
    ("货物", ("货物",)),
    ("服务", ("服务",)),
)


def project_type_from_title(title: str | None) -> str | None:
    """标题确定性种类抽取：命中 → 施工/EPC总承包/监理/设计/勘察/货物/服务；未命中 → None。"""
    t = title or ""
    for kind, words in _TITLE_KINDS:
        for word in words:
            if word in t:
                return kind
    return None


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


# ── 已注册源（2026-09-08 逐源 web 实测校准） ────────────────────────────
# 验证方法：urllib 探测列表页 HTTP+内容结构；verified = 200+含真实公告条目（static）
SOURCES: dict[str, SourceSpec] = {
    # ========== 惠招标（河北交投，L1，实测可解析，4 分区） ==========
    "hebtig": SourceSpec(
        source_id="hebtig", name="惠招标（河北交投）工程类", level="L1",
        base_url="https://ebidding.hebtig.com", list_path="/jyxx/001001/001001001/trade.html",
        category="招标专区-工程类", region_scope="河北省", admin_subregions=_HEBEI_SUBREGIONS,
        search_param="keyword", page_param="path_page", max_pages=6,
        collectable=True, compliance_status="verified",
    ),
    "hebtig_service": SourceSpec(
        source_id="hebtig_service", name="惠招标（河北交投）服务类", level="L1",
        base_url="https://ebidding.hebtig.com", list_path="/jyxx/001001/001001003/trade.html",
        category="招标专区-服务类", region_scope="河北省", admin_subregions=_HEBEI_SUBREGIONS,
        search_param="keyword", page_param="path_page", max_pages=6,
        collectable=True, compliance_status="verified",
    ),
    "hebtig_goods": SourceSpec(
        source_id="hebtig_goods", name="惠招标（河北交投）货物类", level="L1",
        base_url="https://ebidding.hebtig.com", list_path="/jyxx/001001/001001004/trade.html",
        category="招标专区-货物类", region_scope="河北省", admin_subregions=_HEBEI_SUBREGIONS,
        search_param="keyword", page_param="path_page", max_pages=6,
        collectable=True, compliance_status="verified",
    ),
    "hebtig_nzb": SourceSpec(
        source_id="hebtig_nzb", name="惠招标（河北交投）非招标专区", level="L1",
        base_url="https://ebidding.hebtig.com", list_path="/jyxx/001002/001002001/trade.html",
        category="非招标专区", region_scope="河北省", admin_subregions=_HEBEI_SUBREGIONS,
        search_param="keyword", page_param="path_page", max_pages=6,
        collectable=True, compliance_status="verified",
    ),

    # ========== 国家级（L1） =========
    "ggzy": SourceSpec(
        source_id="ggzy", name="全国公共资源交易平台", level="L1",
        base_url="https://www.ggzy.gov.cn", list_path="/",
        category="招标公告", region_scope="全国", collectable=True,
        compliance_status="verified",  # 2026-09-08 实测根域 200
    ),
    "ccgp": SourceSpec(
        source_id="ccgp", name="中国政府采购网", level="L1",
        base_url="http://www.ccgp.gov.cn", list_path="/cggg/dfgg/",
        category="招标公告", region_scope="全国", collectable=True,
        compliance_status="verified",  # 2026-09-08 /cggg/dfgg/ 200
        search_param="gp", page_param="pageNo",
    ),
    "cebpubservice": SourceSpec(
        source_id="cebpubservice", name="中国招标投标公共服务平台", level="L1",
        base_url="https://www.cebpubservice.com", list_path="/",
        category="招标公告", region_scope="全国", collectable=True,
        compliance_status="pending",  # 公告列表在 JS 子页(bulletin.cebpubservice.com 动态加载)，urllib 无 JS 渲染
    ),

    # ====== 省级京津冀（L1a:实测） ======
    "ggzyfw-beijing": SourceSpec(
        source_id="ggzyfw-beijing", name="北京公共资源交易服务平台", level="L1",
        base_url="https://ggzyfw.beijing.gov.cn", list_path="/",
        category="招标公告", region_scope="北京市", collectable=True,
        compliance_status="pending",  # SSL BAD_ECPOINT（服务器 TLS 旧）；内容存在、浏览器握手受限
    ),
    "ccgp-beijing": SourceSpec(
        source_id="ccgp-beijing", name="北京市政府采购网", level="L1",
        base_url="http://www.ccgp-beijing.gov.cn", list_path="/",
        category="招标公告", region_scope="北京市", collectable=True,
        compliance_status="verified",  # 2026-09-08 实测静态列表 200 + 67 条公告
    ),
    "ccgp-tianjin": SourceSpec(
        source_id="ccgp-tianjin", name="天津市政府采购网", level="L1",
        base_url="http://www.ccgp-tianjin.gov.cn", list_path="/",
        category="招标公告", region_scope="天津市", collectable=True,
        compliance_status="verified",  # 2026-09-08 实测静态列表 200 + 223 条公告
    ),
    "ggzy-tianjin": SourceSpec(
        source_id="ggzy-tianjin", name="天津市公共资源交易平台", level="L1",
        base_url="https://ggzy.zwfwb.tj.gov.cn", list_path="/p54/jyxxgcjs.html",
        category="招标公告", region_scope="天津市", collectable=True,
        compliance_status="pending",  # 公告列表 JS/框架渲染（页面 HTML 仅骨架~15KB），urllib 无 JS 渲染
    ),
    "ccgp-hebei-web": SourceSpec(
        source_id="ccgp-hebei-web", name="河北省政府采购网", level="L1",
        base_url="http://www.ccgp-hebei.gov.cn", list_path="/province/cggg/zbgg/",
        category="招标公告", region_scope="河北省", admin_subregions=_HEBEI_SUBREGIONS,
        collectable=True, compliance_status="verified",
        # 2026-09-08 实测 zbgg/ 200 + 公告条目；全站 robots Disallow（ADR-003 预检留痕不休）
    ),
    "szj-hebei": SourceSpec(
        source_id="szj-hebei", name="河北省公共资源交易服务平台", level="L1",
        base_url="https://www.hbggzyfwpt.cn", list_path="/jyxx/index",
        category="交易信息-工程建设",
        region_scope="河北省", admin_subregions=_HEBEI_SUBREGIONS,
        collectable=True, compliance_status="verified",
        # 2026-09-10 B1：列表入口由门户首页（导航/通知混杂，日期覆盖 5%）改为
        # 交易信息页（静态条目 + 行内日期 span，实测 15+ 条带日期）；
        # szj.hebei.gov.cn/hbggfwpt/ 首页官方链接指向本交易门户（同一平台）。
    ),
    "zhjy-bcactc": SourceSpec(
        source_id="zhjy-bcactc", name="北京建设工程交易系统", level="L1",
        base_url="https://zhjy.bcactc.com", list_path="/",
        category="房建/市政", region_scope="北京市", collectable=True,
        compliance_status="pending",  # SSL BAD_ECPOINT（服务器 TLS 旧）；需备选入口
    ),

    # ====== 河北地市（L2，实测；parent_region=河北省 → 省级检索覆盖，C1） ======
    "sjzsggzy": SourceSpec(
        source_id="sjzsggzy", name="石家庄市公共资源交易中心", level="L2",
        base_url="https://www.sjzsggzyjyzx.org.cn", list_path="/jyxxgczb/index.jhtml",
        category="交易信息-招标公告", region_scope="石家庄市", parent_region="河北省",
        collectable=True,
        compliance_status="verified",
        # 2026-09-10 B1：入口由首页（新闻通知，日期覆盖 0%）改为工程招标频道
        # （静态条目 + 行内 <div>2026.09.10</div> 日期，实测 10 条带日期）
    ),
    "tsggzy": SourceSpec(
        source_id="tsggzy", name="唐山市公共资源交易中心", level="L2",
        base_url="http://ggzyjy.xzspj.tangshan.gov.cn", list_path="/",
        category="招标公告", region_scope="唐山市", parent_region="河北省",
        collectable=True,
        compliance_status="verified",  # 2026-09-08 实测静态列表 200 + 37 条公告
    ),
    "hdggzy": SourceSpec(
        source_id="hdggzy", name="邯郸市公共资源交易中心", level="L2",
        base_url="https://ggzy.hd.gov.cn",
        list_path="/jydt/003002/003002001/trading_hall.html",
        category="工程建设-招标/资审公告",
        region_scope="邯郸市", parent_region="河北省",
        collectable=True, compliance_status="verified",
        # 2026-09-10 A2：本机 DNS/TLS/HTTP 探测通过（200）→ 接入工程招标频道；
        # 实测静态条目：锚内 <span class="inf">标题</span><span class="date">2026-09-09</span>
        # + URL 日期段 /20260909/，日期双路可提取。旧入口 "/" 为新闻通知混杂页。
    ),
    "qhdggzy": SourceSpec(
        source_id="qhdggzy", name="秦皇岛市公共资源交易中心", level="L2",
        base_url="http://www.qhdggzy.cn/qhdggzy/", list_path="/",
        category="招标公告", region_scope="秦皇岛市", parent_region="河北省",
        collectable=False,
        compliance_status="blocked",  # 2026-09-08 探测 404（入口未确认，暂不采）
    ),
    "cdggzy": SourceSpec(
        source_id="cdggzy", name="承德市公共资源交易中心", level="L2",
        base_url="http://szj.chengde.gov.cn", list_path="/cdsggzy/",
        category="招标公告", region_scope="承德市", parent_region="河北省",
        collectable=True,
        compliance_status="verified",  # 2026-09-08 实测静态列表 200 + 154 条公告
    ),
    "hsggzy": SourceSpec(
        source_id="hsggzy", name="衡水市公共资源交易中心", level="L2",
        base_url="http://hsggzy.hengshui.gov.cn", list_path="/",
        category="招标公告", region_scope="衡水市", parent_region="河北省",
        collectable=True,
        compliance_status="verified",  # 2026-09-08 实测静态列表 200 + 182 条公告
    ),
    "cangzhou": SourceSpec(
        source_id="cangzhou", name="沧州市公共资源交易中心", level="L2",
        base_url="https://xzsp.cangzhou.gov.cn", list_path="/xzsp/add100115/",
        category="招标公告", region_scope="沧州市", parent_region="河北省",
        collectable=False, compliance_status="pending",
        # 2026-09-10 A2：修复 list_path 缺前导斜杠的拼接缺陷（旧值 "xzsp/add100115/"
        # 拼出 https://xzsp.cangzhou.gov.cnxzsp/… 不可达）。实测本路径为门户新闻页，
        # 公告列表由 JS 动态填充（<ul id="trade11"> 空容器）→ 如实标 pending、
        # 暂不采集；静态接口（listDisplaySelf 仅政务新闻）待校准后再启用。
    ),
    "xingtai": SourceSpec(
        source_id="xingtai", name="邢台市公共资源交易中心", level="L2",
        base_url="http://60.6.198.121:8888", list_path="/sszt-zyjyPortal/",
        category="招标公告", region_scope="邢台市", parent_region="河北省",
        collectable=False,
        compliance_status="blocked",  # 2026-09-08 探测 404；JS 门户（IP 直连不可达）
    ),

    # ====== L0 第三方（需签约，仅登记） ======
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


# C3（2026-09-10）：地区下拉由注册表生成——河北省 + 真有可采集源（collectable）的
# 地市；无源城市不出现（选了也只会得到放宽召回的空结果，徒增困惑）。
_REGION_ORDER = (
    "石家庄市", "唐山市", "秦皇岛市", "邯郸市", "邢台市", "保定市",
    "张家口市", "承德市", "沧州市", "廊坊市", "衡水市",
    "雄安新区", "定州市", "辛集市",
)


def region_options() -> list[str]:
    """搜索表单地区选项：[河北省] + 有可采集源的地市（按地理序）。"""
    cities: set[str] = set()
    for s in SOURCES.values():
        if not s.collectable:
            continue
        if s.region_scope != _DEFAULT_REGION and (s.region_scope in _REGION_ORDER):
            cities.add(s.region_scope)
    return [_DEFAULT_REGION] + [c for c in _REGION_ORDER if c in cities]


def validate_sources(sources: list[str] | None) -> tuple[list[str], list[str]]:
    """校验请求中的 sources：返回 (有效, 未知)，并按首次顺序去重。
    空/None → 全部已注册源。blocked/collectable=False 的源仅登记，validate 视为有效，
    采集侧按 collectable 过滤是否实际抓取。
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