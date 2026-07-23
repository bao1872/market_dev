
const PAGE_INDEX=[
 {title:"使用说明首页",path:"index.html",keywords:"首页 总览 使用说明 功能 入口"},
 {title:"快速开始",path:"pages/quick-start.html",keywords:"邀请码 注册 登录 页面 第一次使用"},
 {title:"行情查看",path:"pages/market.html",keywords:"搜索 股票 周期 列表 图表"},
 {title:"自选管理",path:"pages/watchlist.html",keywords:"加入 移除 自选 数量 权限"},
 {title:"个股详情",path:"pages/stock-detail.html",keywords:"详情 数据 字段 图表 状态"},
 {title:"提醒记录",path:"pages/alerts.html",keywords:"提醒 条件 历史 记录"},
 {title:"消息通知",path:"pages/messages.html",keywords:"站内消息 通知 设置 失败"},
 {title:"指标原理",path:"pages/data.html",keywords:"筹码共识 价格结构 成交聚集 指标 原理"},
 {title:"常见问题",path:"pages/faq.html",keywords:"问题 排查 账户 功能 邀请码"},
 {title:"使用边界",path:"pages/boundaries.html",keywords:"边界 风险 数据 建议"},
 {title:"量化定制",path:"pages/customization.html",keywords:"因子 量化 定制 回测 筛选 监控 新因子 已交付案例"}
];
const body=document.body;const root=body.dataset.root||".";const resolvePath=path=>root==="."?path:`${root}/${path}`;const menuToggle=document.querySelector("[data-menu-toggle]");const backdrop=document.querySelector(".mobile-backdrop");
function closeMenu(){body.classList.remove("menu-open");menuToggle?.setAttribute("aria-expanded","false")}
menuToggle?.addEventListener("click",()=>{const open=body.classList.toggle("menu-open");menuToggle.setAttribute("aria-expanded",String(open))});backdrop?.addEventListener("click",closeMenu);document.querySelectorAll(".nav-item").forEach(item=>item.addEventListener("click",closeMenu));
document.querySelectorAll(".faq-button").forEach(btn=>btn.addEventListener("click",()=>{const item=btn.closest(".faq-item");const open=item.classList.toggle("open");btn.setAttribute("aria-expanded",String(open))}));
const search=document.querySelector(".doc-search"),results=document.querySelector(".search-results");function renderSearch(value){const q=value.trim().toLowerCase();if(!q){results?.classList.remove("open");if(results)results.innerHTML="";return}const matches=PAGE_INDEX.filter(item=>(item.title+" "+item.keywords).toLowerCase().includes(q));if(results){results.innerHTML=matches.length?matches.map(item=>`<a class="search-result" href="${resolvePath(item.path)}">${item.title}</a>`).join(""):`<div class="search-result">没有找到相关页面</div>`;results.classList.add("open")}}
search?.addEventListener("input",e=>renderSearch(e.target.value));search?.addEventListener("focus",e=>renderSearch(e.target.value));document.addEventListener("click",e=>{if(!e.target.closest(".search-wrap"))results?.classList.remove("open")});
const articleLinks=[...document.querySelectorAll("[data-anchor]")];const sections=articleLinks.map(a=>document.querySelector(a.getAttribute("href"))).filter(Boolean);if("IntersectionObserver" in window&&sections.length){const observer=new IntersectionObserver(entries=>entries.forEach(entry=>{if(entry.isIntersecting)articleLinks.forEach(a=>a.classList.toggle("active",a.getAttribute("href")==="#"+entry.target.id))}),{rootMargin:"-18% 0px -72% 0px"});sections.forEach(section=>observer.observe(section))}
