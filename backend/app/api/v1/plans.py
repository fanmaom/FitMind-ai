"""计划导入路由。

两个端点对应两步：先 preview 看清楚，再 commit 落库。中间不留服务端状态——
预览结果不缓存、不入库，第二步重新上传同一个文件。

无状态是有意的。缓存预览需要一个带过期的临时存储，还要处理"用户在预览页停留
半小时后才点确认"这类情况；而重传一个几十 KB 的 xlsx 成本几乎为零。
"""

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select

from app.api.deps import RequestContext, get_context
from app.core.domain.plan_import import PlanParseError, parse_carb_cycle_workbook
from app.core.logger import logger
from app.models.plan import Plan
from app.services.plan_import import build_preview, commit_import

router = APIRouter(prefix="/api/v1/plans", tags=["plans"])

# 上传体积上限。
#
# 一份 4 周计划是 13 KB，留到 2 MB 已经宽到能装几年的周期表。设这道闸是因为
# openpyxl 解析在内存里展开，几十 MB 的表格能把整个 worker 拖垮——而拒绝一个
# 超大文件的代价只是用户看到一句提示。
MAX_UPLOAD_BYTES = 2 * 1024 * 1024

# 只认 xlsx。旧的 .xls 是完全不同的二进制格式，openpyxl 读不了；csv 没有多表
# 结构，装不下"每周一张表"。与其含糊地失败，不如明确告诉用户该怎么转。
ALLOWED_SUFFIXES = (".xlsx", ".xlsm")


async def _read_upload(file: UploadFile) -> bytes:
    name = (file.filename or "").lower()
    if not name.endswith(ALLOWED_SUFFIXES):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "只支持 .xlsx 文件。如果你手上是旧的 .xls 或 .csv，"
            "用 Excel／WPS 打开后「另存为 xlsx」再上传。",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件是空的")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB，请确认上传的是计划表。",
        )
    return data


def _parse_or_400(data: bytes):
    try:
        return parse_carb_cycle_workbook(data)
    except PlanParseError as exc:
        # 解析失败是用户的文件问题，不是服务端故障——用 400 而不是 500，
        # 并且把解析器写好的那句人话原样透出去。日志留 info 级别：
        # 这类失败会正常发生，堆在 error 里会淹掉真正的故障。
        logger.info(f"计划解析失败：{exc}")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("")
async def list_plans(ctx: RequestContext = Depends(get_context)) -> list[dict]:
    """列出我的计划。

    只回摘要，不回完整的 weeks——一份 8 周计划展开有 56 天 × 6 个字段，
    列表页用不上，而且这个接口会被前端在每次导入后刷新。明细走
    get_plan_day / get_plan_detail。
    """
    rows = await ctx.session.scalars(
        select(Plan).where(Plan.user_id == ctx.user_id).order_by(Plan.created_at.desc()),
    )
    result = []
    for plan in rows:
        payload = plan.payload or {}
        weeks = payload.get("weeks", [])
        result.append({
            "id": str(plan.id),
            "type": plan.type,
            "status": plan.status,
            "source": payload.get("source", "generated"),
            "source_name": payload.get("source_name"),
            "week_count": len(weeks),
            "day_count": sum(len(w.get("days", [])) for w in weeks),
            "carb_cycle": bool(payload.get("carb_cycle")),
            "created_at": plan.created_at.isoformat(),
        })
    return result


@router.post("/import/preview")
async def preview_plan_import(
    file: UploadFile = File(..., description="计划表 xlsx"),
    ctx: RequestContext = Depends(get_context),
) -> dict:
    """只解析、不落库。

    存在这一步是因为导入会覆盖档案里的身体数据，而网上流传的模板都带着原作者的
    参数（样例文件是 91kg / 180cm / 25 岁）。直接写进去等于把别人的身体数据变成
    用户自己的，而档案每轮注入 prompt，之后所有热量计算都会按错的体重算。

    顺带也是唯一能在数据进库前发现解析错位的机会。
    """
    data = await _read_upload(file)
    parsed = _parse_or_400(data)
    preview = build_preview(parsed)
    logger.info(
        f"计划预览 user={ctx.user_id} file={file.filename} "
        f"weeks={preview.week_count} days={preview.day_count} "
        f"warnings={len(preview.warnings)}",
    )
    return preview.to_response()


@router.post("/import", status_code=201)
async def commit_plan_import(
    file: UploadFile = File(..., description="计划表 xlsx"),
    apply_profile: bool = Form(
        default=False, description="是否把文件里的身高体重写进我的档案",
    ),
    ctx: RequestContext = Depends(get_context),
) -> dict:
    """落库。

    apply_profile 默认 False——档案是用户身份的一部分，默认不动。
    """
    data = await _read_upload(file)
    parsed = _parse_or_400(data)
    return await commit_import(
        ctx.session, ctx.user_id, parsed,
        apply_profile=apply_profile,
        source_name=file.filename or "",
    )
