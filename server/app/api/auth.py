"""鉴权接口：邀请码 + 设备注册、refresh 轮换、登出。

多 refresh token 模型（threads5 P1-2，INV-8）：凭据一行一枚存在
refresh_tokens 表——同设备同账号并发登录各自追加一行、互不覆盖，每个
200 响应返回的 refresh token 在过期 / 被显式作废（轮换、设备吊销、
登出）/ 被每设备 5 枚上限挤出之前都能用。轮换只作废被轮换的那一枚；
重放已作废的 token 按复用吊销整台设备（原有语义）。

锁序（INV-1）：device → refresh_tokens（login 经 device 原子 upsert 先
锁 device 行；refresh 先 PG FOR UPDATE device 行再条件更新 refresh_tokens），
不碰 sequences。
"""
from fastapi import APIRouter, Depends
from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import (
    create_access_token,
    hash_refresh_token,
    new_refresh_token,
    refresh_expiry,
)
from app.common import new_id
from app.db import get_db_session
from app.deps import get_auth
from app.errors import ApiError
from app.jobs.queue import is_postgres
from app.models import Device, InviteCode, RefreshToken, User
from app.schemas import LoginRequest, RefreshRequest
from app.timeutil import as_utc, utcnow

router = APIRouter(prefix="/v1/auth", tags=["auth"])

# 每台设备保留的未过期未作废 refresh token 上限（INV-8）：登录/轮换追加
# 新行后修剪最老的超限行；修剪条件排除本次返回的行——本次凭证一定幸存
_REFRESH_TOKENS_PER_DEVICE = 5


def _token_pair(user_id: str, device_id: str, generation: int) -> dict:
    """access 的 gen claim 与新 refresh 行都携带当前设备凭证代（INV-8
    陈述 4）：调用方负责传「device 行锁落定之后」读到的代。"""
    access, expires_in = create_access_token(user_id, device_id, generation)
    return {
        "access_token": access,
        "refresh_token": new_refresh_token(),
        "expires_in": expires_in,
        "user_id": user_id,
    }


def _device_upsert(device_id: str, user_id: str, body: LoginRequest, now):
    """单条 INSERT … ON CONFLICT DO UPDATE（INV-8，修 C1）：原子完成
    「存在性检查 + 归属检查 + 复活/刷新」（不再写任何 refresh 哈希列——
    凭据在 refresh_tokens 表，见模块 docstring）。

    - 无冲突 → 插入新设备行；
    - 冲突且 user_id 相同 → DO UPDATE 清 revoked、刷新 last_seen
      （name/platform 仅在请求非空时覆盖）；
    - 冲突且 user_id 不同 → WHERE 不成立，rowcount=0 → 调用方复查后 403。

    并发语义：同设备登录在行/PK 上串行化（后面的 refresh_tokens 追加与
    修剪因此天然排在 device 行锁之后，无并发互删窗口）。
    """
    if is_postgres():
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        insert_stmt = pg_insert(Device)
    else:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        insert_stmt = sqlite_insert(Device)
    stmt = insert_stmt.values(
        id=device_id,
        user_id=user_id,
        name=body.device_name or "",
        platform=body.platform or "android",
        last_seen=now,
    )
    excluded = stmt.excluded
    return stmt.on_conflict_do_update(
        index_elements=[Device.id],
        set_={
            "revoked_at": None,
            # INV-8 陈述 4（threads12 P1）：真复活（旧行 revoked_at 非空）
            # 才 +1 代——普通再登录不涨（否则并发登录互相把对方刚签发的
            # 凭证打成跨代）。SET 里按表名（devices）引用的是冲突前的
            # **旧行**值，PG 与 SQLite 的 upsert 语义一致
            "credential_generation": case(
                (Device.revoked_at.is_not(None), Device.credential_generation + 1),
                else_=Device.credential_generation,
            ),
            "last_seen": now,
            # 请求里为空的 name/platform 不覆盖已有值
            "name": func.coalesce(func.nullif(excluded.name, ""), Device.name),
            "platform": func.coalesce(func.nullif(excluded.platform, ""), Device.platform),
        },
        # 归属检查放进语句：设备已绑定其他账号时不更新（rowcount=0 → 403）
        where=Device.user_id == excluded.user_id,
    )


async def _append_refresh_token(
    session: AsyncSession, device_id: str, user_id: str, token: str, now, generation: int
) -> None:
    """追加一枚 refresh token 并顺手清理设备上的陈旧行（INV-8）。

    调用方已完成 device 行 upsert/加锁（锁序 device → refresh_tokens）。
    清理两类（threads6 P2）：
    - **已过期**（expires_at <= now，无论是否作废）：留着对任何路径都
      无意义——重放只会得到与「行不存在」等价的普通 401，却让表随时间
      无限膨胀；
    - **未过期未作废**里最新的 4 枚之外的最老行（原每设备上限修剪）。

    **已作废但未过期**的行保留到过期为止：这正是重放检测的依据（轮换
    掉的 token 在有效期内被重放要吊销整台设备）。修剪条件排除本行：
    本次返回的 token 一定保留；并发登录在 device 行锁上串行化，修剪不会
    误删别的已提交响应的 token。
    """
    row = RefreshToken(
        id=new_id("rtk"),
        device_id=device_id,
        user_id=user_id,
        token_hash=hash_refresh_token(token),
        created_at=now,
        expires_at=refresh_expiry(),
        # 行的代在插入时写定、此后不可变（INV-8 陈述 4）：refresh 只对
        # 「行的代 == 设备当前代」的行轮换
        credential_generation=generation,
    )
    session.add(row)
    await session.flush()
    live = (
        RefreshToken.device_id == device_id,
        RefreshToken.id != row.id,
        RefreshToken.revoked_at.is_(None),
        RefreshToken.expires_at > now,
    )
    keep = (
        select(RefreshToken.id)
        .where(*live)
        .order_by(RefreshToken.created_at.desc(), RefreshToken.id.desc())
        .limit(_REFRESH_TOKENS_PER_DEVICE - 1)
    )
    await session.execute(
        delete(RefreshToken)
        .where(
            RefreshToken.device_id == device_id,
            RefreshToken.id != row.id,
            # 已过期一律删；未过期的只做每设备上限修剪（作废行留到过期）
            or_(
                RefreshToken.expires_at <= now,
                and_(RefreshToken.revoked_at.is_(None), RefreshToken.id.not_in(keep)),
            ),
        )
        .execution_options(synchronize_session=False)
    )


async def _lock_device(session: AsyncSession, device_id: str) -> Device | None:
    """按锁序取 device 行：PG 下 FOR UPDATE 串行化同设备的轮换/吊销。"""
    stmt = select(Device).where(Device.id == device_id)
    if is_postgres():
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def _revoke_device_for_reuse(
    session: AsyncSession, device_id: str, token_row_id: str
):
    """重放已作废（被轮换/吊销）的 refresh：吊销设备并作废其全部 token
    （原有语义保留）。锁序：device → refresh_tokens。

    threads7 P1（auth.py:246，INV-8）：首读（无锁快照）看到 revoked_at
    非空**不足以**直接吊销——等锁期间该行可能已被 logout 删除、或已跨过
    expires_at（尚未被修剪），这两类按 INV-8 都是普通 401、不吊销设备。
    先 _lock_device 串行化同设备的轮换/吊销，锁内用标量列重读该行归因
    （绕过 identity map，INV-12：本会话首读装载的 ORM 对象不随并发提交
    刷新），按固定顺序分类：不存在→401、已过期→401、已作废→重放吊销。"""
    await session.rollback()
    device = await _lock_device(session, device_id)
    if device is None:
        # 设备行不存在（理论不可达，当前无删除路径）：无从吊销，普通 401
        raise ApiError(401, "unauthorized", "invalid refresh token")
    current = (
        await session.execute(
            select(
                RefreshToken.id,
                RefreshToken.expires_at,
                RefreshToken.revoked_at,
                RefreshToken.credential_generation,
            ).where(RefreshToken.id == token_row_id)
        )
    ).first()
    if current is None:
        # 等锁期间行被 logout 删除 / 第 6 次登录挤出：普通 401，不吊销设备
        raise ApiError(401, "unauthorized", "invalid refresh token")
    if as_utc(current.expires_at) <= utcnow():
        # 等锁期间跨过过期时刻（过期行尚未被修剪）：按过期 401，不吊销设备
        raise ApiError(401, "unauthorized", "refresh token 已过期")
    if current.revoked_at is None:
        # 理论不可达：revoked_at 只置不清，首读看到的是已提交值，不随开头
        # 的 rollback 撤销。证据消失时绝不吊销设备（防御性普通 401）
        raise ApiError(401, "unauthorized", "invalid refresh token")
    # INV-8 陈述 4（threads12 P1）：跨代的行是吊销期签发的旧凭证（行的代
    # 不可变，锁前锁后读都一样；设备代在锁内已重读）。重放它只能普通 401
    # ——若进吊销分支，被盗的旧 token 每次都能把重新登录的新会话再吊销
    # 一遍（连环 DoS）。不轮换、不吊销
    if current.credential_generation != device.credential_generation:
        raise ApiError(401, "unauthorized", "invalid refresh token")
    device.credential_generation += 1
    device.revoked_at = utcnow()
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.device_id == device_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    raise ApiError(401, "refresh_reused", "检测到 refresh token 复用，设备已吊销")


async def _claim_invite_first_use(
    session: AsyncSession, invite_code: str, user_id: str, now
) -> bool:
    """首用抢占（原 login 内联）：条件 UPDATE 把 owner_user_id 从 NULL 置为
    本事务用户 id、used+1，WHERE 同时原子判定未用尽与未过期。rowcount!=0
    即抢占成功（调用方创建账号）；0 行 → 调用方走 _resolve_lost_invite。

    INV-13（threads10 P1，auth.py:211）：`expires_at > :now` 是**语句参数**，
    PG READ COMMITTED 下这条 UPDATE 在 invite 行锁上等待别的事务时，锁
    释放后用等待前绑定的 now 重评谓词——等待期间跨过 expires_at 的码仍
    rowcount=1 抢占成功，绕过输方归因直接创建账号。行值谓词（owner/used）
    由 UPDATE 对行当前值原子判定，无此问题；只有墙钟绑定参数会陈旧。
    故抢占成功后（仍持行锁）标量列重读 expires_at、重取 utcnow() 复验；
    已过期 → 回滚本次 claim（owner/used 撤销）并按过期 403，不签发凭证。"""
    claimed = await session.execute(
        update(InviteCode)
        .where(
            InviteCode.code == invite_code,
            InviteCode.owner_user_id.is_(None),
            InviteCode.used < InviteCode.max_uses,
            or_(InviteCode.expires_at.is_(None), InviteCode.expires_at > now),
        )
        .values(owner_user_id=user_id, used=InviteCode.used + 1)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount == 0:
        return False
    # 成功后复验：谓词里的 now 是等锁前绑定的，rowcount=1 不代表仍未过期
    # （标量列重读绕过 identity map，INV-12；不 rollback 的重读必须如此）
    expires_at = (
        await session.execute(
            select(InviteCode.expires_at).where(InviteCode.code == invite_code)
        )
    ).scalar()
    if expires_at is not None and as_utc(expires_at) <= utcnow():
        await session.rollback()
        raise ApiError(403, "invalid_invite", "邀请码不存在或已过期")
    return True


async def _resolve_lost_invite(session: AsyncSession, invite_code: str) -> str:
    """并发首登抢占的输方：回滚后重读已提交状态归因（INV-12）。owner 已
    有值 → 登录该 owner（“同码后续登录复用同一账号”的既有语义，而不是
    403：拿着同一个码的设备本就该落到同一个账号）；仍无 owner / 已过期 /
    不存在 → 403。

    INV-13（threads9 P1，auth.py:240）：输方的条件 UPDATE 在 invite 行锁
    上等待赢家事务，输掉后重读到的已是等待之后的世界——过期判定必须用
    **重读之后**重取的 now，沿用等锁之前的旧 now 会接受等待期间跨过
    expires_at 的邀请并登录赢家账号（同一时刻起步的全新登录反而 403）。"""
    await session.rollback()
    invite = await session.get(InviteCode, invite_code)
    now = utcnow()
    if (
        invite is None
        or invite.owner_user_id is None
        or (invite.expires_at is not None and as_utc(invite.expires_at) <= now)
    ):
        raise ApiError(403, "invalid_invite", "邀请码不存在或已过期")
    return invite.owner_user_id


@router.post("/login")
async def login(body: LoginRequest, session: AsyncSession = Depends(get_db_session)):
    invite = await session.get(InviteCode, body.invite_code)
    now = utcnow()
    if invite is None or (invite.expires_at is not None and as_utc(invite.expires_at) <= now):
        raise ApiError(403, "invalid_invite", "邀请码不存在或已过期")

    # 首次使用：创建账号（used 只计注册次数，之后同码登录复用同一账号）。
    # 首用抢占用条件 UPDATE 原子完成（只有把 owner_user_id 从 NULL 置为
    # 本事务用户 id 的事务才创建账号）：并发首登同一码时，其余事务
    # rowcount=0 后复查，按“同码后续登录复用同一账号”的既有语义登录
    # 先占到的人——同一邀请码不会创建出多个用户。加锁顺序上这是唯一
    # 要锁的业务行之一（invite → user → device → refresh_tokens，本接口
    # 不碰 sequences）
    user_id = invite.owner_user_id
    if user_id is None:
        if invite.used >= invite.max_uses:
            raise ApiError(403, "invalid_invite", "邀请码已用完")
        user_id = new_id("usr")
        if await _claim_invite_first_use(session, body.invite_code, user_id, now):
            session.add(User(id=user_id))
            await session.flush()
        else:
            # 并发首登被别的事务抢走（或恰好过期/用尽）：重读归因
            user_id = await _resolve_lost_invite(session, body.invite_code)

    upserted = await session.execute(_device_upsert(body.device_id, user_id, body, now))
    if upserted.rowcount == 0:
        # WHERE 不成立：设备已绑定其他账号。复查确认后 403（行不存在 =
        # 并发删除，当前无删除路径，保守按冲突处理并让部署问题暴露）
        await session.rollback()
        device = await session.get(Device, body.device_id)
        if device is None or device.user_id == user_id:
            raise ApiError(500, "internal_error", "设备行状态异常，登录未落定")
        raise ApiError(403, "device_conflict", "设备已绑定其他账号")
    # INV-13（threads9 P1，auth.py:248）：凭证在 device 行锁落定**之后**
    # 签发——upsert 的行/PK 锁是本接口最长的等待窗口（同设备并发登录在
    # 此串行化），锁前算好的 iat/exp 在短 TTL + 长等待下会让 access token
    # 到手即过期（refresh 路径一直是锁后签发）。锁后重取 now 供签发与
    # 追加行的 created_at/修剪判定；upsert 的 last_seen 用旧 now 是
    # 纯展示边界（无任何决策读它）
    now = utcnow()
    # INV-13（threads14 P1，auth.py:334）：上面的全部 invite 过期判定——
    # 入口预检、首用 claim 后复验、输方重读归因——用的都可能是等 device
    # 行锁**之前**的时间；upsert 的行/PK 锁是签发前最后一个（也是最长的）
    # 等待窗口，等待期间跨过 expires_at 的邀请码不得发证。锁落定后用新
    # now 复验（标量列重读绕过 identity map，INV-12；与 claim 成功后复验、
    # 输方重读后重取 now 同口径）：已过期 → 回滚本次登录事务（device
    # upsert、首用 claim 连同账号创建一并撤销）并 403。首用与既有 owner
    # 登录两条路径都走这里
    invite_expires_at = (
        await session.execute(
            select(InviteCode.expires_at).where(InviteCode.code == body.invite_code)
        )
    ).scalar()
    if invite_expires_at is not None and as_utc(invite_expires_at) <= now:
        await session.rollback()
        raise ApiError(403, "invalid_invite", "邀请码不存在或已过期")
    # upsert 落定后标量列重读当前代（同事务、同连接，读到的是本事务刚
    # 写入的值；标量列不走 identity map）。真复活时 upsert 已 +1——
    # 吊销前签发的旧凭证（gen 落后）从此被 fence 挡在外面
    generation = (
        await session.execute(
            select(Device.credential_generation).where(Device.id == body.device_id)
        )
    ).scalar()
    result = _token_pair(user_id, body.device_id, generation)
    # 登录只追加：本次返回的 token 与既有各行互不覆盖（INV-8）
    await _append_refresh_token(
        session, body.device_id, user_id, result["refresh_token"], now, generation
    )
    await session.commit()
    return result


@router.post("/refresh")
async def refresh(body: RefreshRequest, session: AsyncSession = Depends(get_db_session)):
    h = hash_refresh_token(body.refresh_token)
    now = utcnow()
    row = (
        await session.execute(select(RefreshToken).where(RefreshToken.token_hash == h))
    ).scalar_one_or_none()

    if row is not None:
        if row.revoked_at is not None:
            # 已被轮换/吊销的 token 重放：复用检测候选——但是否真吊销由
            # _revoke_device_for_reuse 在 device 锁内重读该行后判定（见其
            # docstring：等锁期间行可能已被删除/已过期，都是普通 401）
            return await _revoke_device_for_reuse(session, row.device_id, row.id)
        if as_utc(row.expires_at) <= now:
            # TTL 落库强制（README 已知限制中「有效期未强制」就此关闭）
            raise ApiError(401, "unauthorized", "refresh token 已过期")
        # 锁序（INV-1）：先锁 device 行（PG FOR UPDATE 串行化轮换与吊销、
        # 复查吊销态），再条件更新 refresh_tokens
        device = await _lock_device(session, row.device_id)
        if device is None or device.revoked_at is not None:
            raise ApiError(401, "unauthorized", "device revoked")
        # INV-8 陈述 4（threads12 P1）：行的代（插入时写定、不可变）必须
        # 等于设备当前代。跨代 = 吊销期签发的旧凭证在复活后重放：普通 401
        # ——不轮换（旧凭证不能换新凭证）、也不进复用吊销分支（新会话
        # 不能被连环吊销）
        if row.credential_generation != device.credential_generation:
            raise ApiError(401, "unauthorized", "invalid refresh token")
        # INV-13（threads8 P1，auth.py:294）：开头的 now 是无锁首读时刻，
        # 等锁期间 token 可能已跨过 expires_at——锁内重取 now，让下面的
        # 条件 UPDATE 用行内当前 expires_at 原子判定（条件 UPDATE 的 WHERE
        # 即「锁内重读」）；过期的 token 不得被轮换成新凭证
        now = utcnow()
        # 轮换只作废被轮换的那一枚：条件 UPDATE 原子完成，同一枚 token
        # 并发刷新时只有一个能命中
        device_id, token_user_id = row.device_id, row.user_id
        result = _token_pair(token_user_id, device_id, device.credential_generation)
        rotated = await session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.id == row.id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > now,
            )
            .values(revoked_at=now)
            .execution_options(synchronize_session=False)
        )
        if rotated.rowcount == 1:
            device.last_seen = now
            await _append_refresh_token(
                session,
                device_id,
                token_user_id,
                result["refresh_token"],
                now,
                device.credential_generation,
            )
            await session.commit()
            return result
        # rowcount==0 不能直接断定为重放（threads6 P1-3 / INV-8/INV-12）：
        # 首次读行（无锁）与拿到 device 行锁之间存在窗口，期间这行可能已被
        # 第 6 次登录的上限修剪或 logout 删除（挤出/登出的既定语义是普通
        # 401、不吊销设备），也可能恰好跨过 expires_at。锁内重读该行归因：
        # INV-12——上面的条件 UPDATE 用 synchronize_session=False，不会更新
        # 本会话已加载的 row 对象；这里若 select(RefreshToken) 会命中
        # identity map 里首读的旧对象（revoked_at=None），把并发轮换后的
        # 重放误判成「过期 401」、设备保持活跃。只查标量列：Row 不是实体、
        # 不走 identity map，读到的就是锁内此刻的真值
        current = (
            await session.execute(
                select(RefreshToken.id, RefreshToken.revoked_at).where(
                    RefreshToken.id == row.id
                )
            )
        ).first()
        if current is None:
            # 行已不存在（被挤出/登出删除）：普通 401，不吊销设备
            raise ApiError(401, "unauthorized", "invalid refresh token")
        if current.revoked_at is None:
            # 未作废却没轮换成功：UPDATE 判定时行内 expires_at 已不大于判定
            # 时刻（等锁跨过期——now 已在锁内重取，只剩语句求值到重读之间
            # 的微秒级窗口）→ 按过期 401（不吊销设备）
            raise ApiError(401, "unauthorized", "refresh token 已过期")
        # revoked_at 非空：确系被轮换/吊销后的重放 → 复用检测吊销整台设备
        # （_revoke_device_for_reuse 内部会再按锁内重读复核一次）
        return await _revoke_device_for_reuse(session, device_id, row.id)

    raise ApiError(401, "unauthorized", "invalid refresh token")


@router.post("/logout", status_code=204)
async def logout(
    auth=Depends(get_auth), session: AsyncSession = Depends(get_db_session)
):
    # 登出：清除这台设备的全部 refresh 行（access 到期自然失效，不吊销
    # 设备行）。DELETE 而不是标 revoked：登出后的 token 重放按原有语义
    # 得普通 401（无法关联设备），不触发复用吊销
    #
    # 锁序（INV-1/8，threads11 P1，auth.py:394）：删 refresh_tokens 前先
    # _lock_device（PG FOR UPDATE）。无锁的 session.get 只是读快照、不与
    # 同设备并发 login/refresh 串行化：PG READ COMMITTED 下 DELETE 用语句
    # 开始时的快照，落在对方事务提交前就拿不到它新插入的 token 行——旧
    # 实现实际靠随后的 last_seen UPDATE（device 行锁）隐式挡住了这个窗口，
    # 该写一旦被移除/重排，204 就会漏删并发签发的 token。显式先取 device
    # 行锁使「device → refresh_tokens」的文档锁序成立：logout 必然排在
    # 同设备先行事务之后，DELETE 一定看到其已提交的全部 token 行
    #
    # 锁内代际复验（threads15 P1 / INV-8 陈述 4 + INV-13 总原则）：get_auth
    # 在独立短会话里验过 gen=N，但等这把 device 锁期间设备可能已被吊销
    # （reuse）又复活换代（再登录）——不复查就会删掉**新代**会话的全部
    # refresh 行。锁内重读 Device 的 credential_generation 与 revoked_at
    # （_lock_device 的 SELECT 即锁内读），代已变或设备已吊销 → 本次请求
    # 的凭证所属于的会话已整体失效，按 logout 幂等语义当作「已登出」返回
    # 204、不删任何行——新代 token 的生死由新会话自己决定
    device = await _lock_device(session, auth.device_id)
    if (
        device is not None
        and device.revoked_at is None
        and device.credential_generation == auth.credential_generation
    ):
        device.last_seen = utcnow()
        await session.execute(
            delete(RefreshToken)
            .where(RefreshToken.device_id == auth.device_id)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
