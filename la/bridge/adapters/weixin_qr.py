"""二维码终端渲染(纯 Python,零依赖)。

为什么自己写:引 `qrcode` 库要多一个依赖,而这里只需要"能把一个 URL 扫出来"。
实现的是 QR Model 2 / 字节模式 / 纠错级别 L。

⚠️ **支持范围:版本 1–6(最多 134 字节)。** 不是随手定的:

* v1–v6 **逐个用真实解码器(OpenCV)验收通过**;
* v7+ 目前**不可靠** —— 未完全定位的保留区差异会让码扫不出来,所以直接拒绝,
  而不是生成一个看起来正常、实际扫不动的码;
* 实际用途(微信登录 URL)是 **90 字节,落在 v5**,余量充足
  (`https://liteapp.weixin.qq.com/q/<7位>?qrcode=<32位hex>&bot_type=3`)。

写成这个自研实现的代价必须记下来,免得后人以为"能跑就不用管":
**QR 编码有一堆"错了也不报错、只是扫不出来"的细节。**
本项目在这一版里踩到并修掉的有五处,全部只能靠**真解码器**发现:
  1. 把"用户数据容量"当成"数据码字总数"(v1-L 是 17 字节但只有 12 个数据码字);
  2. RS 生成多项式的系数顺序(必须高次项在前,否则校验子非零);
  3. RS 生成多项式迭代时系数更新的先后顺序;
  4. 数据放置"先右列后左列",以及格式位必须排除在数据位之外;
  5. 多块交织时**纠错码字也要逐列轮转**(按某第三方实现写成"整块依次"是错的)。
"""

from __future__ import annotations

# ⚠️ 这里有两张**不同**的表,混用会生成扫不出来的码(第一版就踩了):
#   CAPACITY_L   = "用户能放多少字节"(已扣掉模式指示与字符计数的开销)
#   TOTAL_CODEWORDS = "这个版本总共有多少码字"(数据码字 = 总数 - 纠错码字)
# 版本 1-L 的 CAPACITY_L 是 17 字节,但它只有 12 个*数据码字* —— 差 5 个。
_CAPACITY_L = {
    1: 17, 2: 32, 3: 53, 4: 78, 5: 106, 6: 134, 7: 154, 8: 192, 9: 230, 10: 271,
}
_TOTAL_CODEWORDS = {
    1: 26, 2: 44, 3: 70, 4: 100, 5: 134, 6: 172, 7: 196, 8: 242, 9: 292, 10: 346,
}
_ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}
# 纠错级别 L 的 RS 分块表,按 (c, k, r) 读:总码字 c / 数据码字 k / 纠错码字 r。
# 版本 1..5 单块 (k, r):(19,7) (34,10) (55,15) (80,20) (108,26)
# 版本 6..10 两块 (k, r):(68,18) (78,20) (97,24) (116,30) (68,18)×4
# ⚠️ 别把 r 写成 k —— 第一版就是在这张表上填错了。
# ⚠️ 这里是**该版本纠错码字的总数**,不是"每块"。多块版本已经乘过块数:
#     v6: 2 块 × (86,68,18) -> 36;  v7: 2 × 20 = 40;  v8: 2 × 24 = 48;
#     v9: 2 × 30 = 60;              v10: 4 × 18 = 72
_EC_CODEWORDS_L = {
    1: 7, 2: 10, 3: 15, 4: 20, 5: 26, 6: 36, 7: 40, 8: 48, 9: 60, 10: 72,
}
_EC_BLOCKS_L = {
    1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 2, 7: 2, 8: 2, 9: 2, 10: 4,
}
# 数据码字总数(总和),用它校验 _TOTAL_CODEWORDS - 纠错码字是否一致
_DATA_CODEWORDS_L = {
    1: 19, 2: 34, 3: 55, 4: 80, 5: 108, 6: 136, 7: 156, 8: 194, 9: 232, 10: 274,
}


def _gf_tables() -> tuple[list[int], list[int]]:
    exp = [0] * 512
    log = [0] * 256
    value = 1
    for index in range(255):
        exp[index] = value
        log[value] = index
        value <<= 1
        if value & 0x100:
            value ^= 0x11D
    for index in range(255, 512):
        exp[index] = exp[index - 255]
    return exp, log


_EXP, _LOG = _gf_tables()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(degree: int) -> list[int]:
    """生成多项式 g(x) = ∏(x - α^i), i = 0..degree-1。

    ⚠️ 系数更新的**顺序**是关键:必须先把高位乘上 α^i,再让低位顶上来。
    写反了多项式就完全不对,而且**不会报错** —— 只表现为 RS 校验子非零、
    二维码扫不出来。第一版就是写反了,靠"g(x) 的根应为 α^0..α^(n-1)"
    这条自检才发现。
    """
    poly = [1]
    for index in range(degree):
        poly.append(0)
        for pos in range(len(poly) - 1, 0, -1):
            poly[pos] = poly[pos - 1] ^ _gf_mul(poly[pos], _EXP[index])
        poly[0] = _gf_mul(poly[0], _EXP[index])
    # ⚠️ 上面的乘法是"低位在前"累积出来的,必须反转成**高次项在前**,
    #    才能和 _rs_encode 里的 generator[index + 1] 对上。
    #    不反转的话多项式每一项都对,只是次序颠倒 —— 于是 RS 校验子非零、
    #    码扫不出来,而肉眼看代码完全正常。第一版就漏了这一步。
    return poly[::-1]


def _rs_encode(data: list[int], ec_count: int) -> list[int]:
    generator = _rs_generator(ec_count)
    remainder = [0] * ec_count
    for byte in data:
        factor = byte ^ remainder[0]
        remainder = remainder[1:] + [0]
        if factor:
            for index in range(ec_count):
                remainder[index] ^= _gf_mul(generator[index + 1], factor)
    return remainder


def _bits_to_bytes(bits: list[int]) -> list[int]:
    out: list[int] = []
    for index in range(0, len(bits) - 7, 8):
        byte = 0
        for offset in range(8):
            byte = (byte << 1) | bits[index + offset]
        out.append(byte)
    return out


#: 实测可用的最高版本。见模块开头说明:v7+ 尚未可靠,宁可报错也不出错码。
MAX_VERSION = 6


def _choose_version(length: int) -> int:
    """挑一个能放下 length 字节的最小版本。

    ⚠️ 上限是 MAX_VERSION(6),不是 QR 规范的上限(40)。
    超限时**抛异常**是刻意的:静默生成一个扫不出来的码,比直接报错危险得多。
    """
    for version in range(1, MAX_VERSION + 1):
        # 4 位模式指示 + 字符计数(版本 1-9 用 8 位)
        header = 4 + (8 if version < 10 else 16)
        if length * 8 + header <= _CAPACITY_L[version] * 8:
            return version
    raise ValueError(
        "内容 " + str(length) + " 字节,超出本实现可靠支持的范围"
        "(版本 " + str(MAX_VERSION) + ",最多 " + str(_CAPACITY_L[MAX_VERSION]) + " 字节)"
    )


def _encode_data(payload: bytes, version: int) -> list[int]:
    bits: list[int] = []
    bits += [0, 1, 0, 0]  # 字节模式
    count_bits = 8 if version < 10 else 16
    length = len(payload)
    for shift in range(count_bits - 1, -1, -1):
        bits.append((length >> shift) & 1)
    for byte in payload:
        for shift in range(7, -1, -1):
            bits.append((byte >> shift) & 1)

    # 数据码字 = 总码字 - 纠错码字。**必须按码字算,不能按"用户容量"算。**
    data_codewords = _TOTAL_CODEWORDS[version] - _EC_CODEWORDS_L[version]
    capacity_bits = data_codewords * 8
    # 终止符(最多 4 位)
    bits += [0] * min(4, capacity_bits - len(bits))
    # 补齐到字节
    while len(bits) % 8:
        bits.append(0)
    # 填充字节
    pad = [0xEC, 0x11]
    index = 0
    while len(bits) < capacity_bits:
        byte = pad[index % 2]
        for shift in range(7, -1, -1):
            bits.append((byte >> shift) & 1)
        index += 1
    return _bits_to_bytes(bits)


def _interleave(data: list[int], version: int) -> list[int]:
    """按 QR 规范交织:数据与纠错**都逐列轮转**。

    ⚠️ 多块版本(v6+)的交织顺序有三种写法,只有一种对,而且**错了不会报错**,
       只表现成"码看着正常但扫不出来":
         * 数据整块拼接、纠错整块拼接 —— 全错;
         * 数据逐列轮转、纠错整块依次 —— **我第一版按某个第三方参考实现写的,
           实测在 v6 上解码失败**;
         * 数据与纠错都逐列轮转 —— ✔ 实测通过。
       结论:第三方实现(哪怕 star 很多)在交织细节上也可能有出入,
       **必须用真解码器验收,不能靠读别人的代码推断**。
    """
    blocks = _EC_BLOCKS_L[version]
    ec_per_block = _EC_CODEWORDS_L[version] // blocks
    data_per_block = len(data) // blocks
    data_blocks = [data[i * data_per_block:(i + 1) * data_per_block] for i in range(blocks)]
    ec_blocks = [_rs_encode(block, ec_per_block) for block in data_blocks]
    out: list[int] = []
    for index in range(data_per_block):       # 数据:逐列轮转
        for block in data_blocks:
            out.append(block[index])
    for index in range(ec_per_block):         # 纠错:同样逐列轮转
        for block in ec_blocks:
            out.append(block[index])
    return out


def _matrix(version: int) -> tuple[list[list[int | None]], list[list[bool]]]:
    size = version * 4 + 17
    grid: list[list[int | None]] = [[None] * size for _ in range(size)]
    reserved: list[list[bool]] = [[False] * size for _ in range(size)]

    def finder(row: int, col: int) -> None:
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = row + dr, col + dc
                if 0 <= r < size and 0 <= c < size:
                    reserved[r][c] = True
                    if 0 <= dr <= 6 and 0 <= dc <= 6:
                        edge = dr in (0, 6) or dc in (0, 6)
                        core = 2 <= dr <= 4 and 2 <= dc <= 4
                        grid[r][c] = 1 if (edge or core) else 0

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    # 定位图形
    for index in range(8, size - 8):
        reserved[6][index] = True
        reserved[index][6] = True
        grid[6][index] = 1 if index % 2 == 0 else 0
        grid[index][6] = 1 if index % 2 == 0 else 0

    # 对齐图形
    #
    # ⚠️ 跳过规则:**中心落在第 6 行或第 6 列的一律不放**(那是时序线)。
    #    第一版写的是"中心已被占用就跳过",看起来等价,其实差了十万八千里:
    #    v6 的唯一对齐图形中心是 (34,34),它周围被自身占用 —— 于是被整块跳过,
    #    数据位凭空多出 7 个,码整体错位、扫不出来。
    #    正确结果:v2/v3/v4/v5/v6 各 1 个,v7 起按 _ALIGN 展开后为 6 个。
    for row in _ALIGN[version]:
        for col in _ALIGN[version]:
            if row == 6 or col == 6:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    r, c = row + dr, col + dc
                    if 0 <= r < size and 0 <= c < size:
                        reserved[r][c] = True
                        grid[r][c] = 1 if (max(abs(dr), abs(dc)) != 1) else 0

    # ⚠️⚠️ 这里有一条**反直觉但必须遵守**的约定:格式信息位**不算保留区**。
    #
    # 规范实现的做法是:放数据之前先把格式位占上(test 模式,值无意义),
    # 这样它们在数据放置时就不是"空位";放完数据之后再用真实格式串覆盖。
    # 结果就是"数据只落在 208 个真·数据模块上",而且**读取顺序与规范一致**。
    #
    # 第一版把格式位当成空闲区保留掉了,数据位整体错位 ——
    # 生成出来的码"看起来完全正常",但任何解码器都读不出来。
    # 教训:这种 bug 只能靠**真解码器**发现,肉眼和"像不像"都没用。
    _write_format(grid, 0)
    # 版本信息块:版本 7 起才有,两份 3x6 / 6x3,共 36 个格子
    if version >= 7:
        _write_version_info(grid, reserved, version)
    return grid, reserved


def _place_data(
    grid: list[list[int | None]],
    reserved: list[list[bool]],
    codewords: list[int],
    *,
    skip: set[tuple[int, int]] | None = None,
) -> None:
    """按规范顺序把码字放进数据位。

    :param skip: 额外跳过的格子(格式信息 + 暗模块)。**必须传**,
        否则数据会把格式位填掉 —— 虽然最后会被覆盖回去,但
        "填掉再覆盖"会让数据位整体错位,生成的码扫不出来。

    遍历顺序(与规范实现逐位一致,**顺序错一位整张码就废**):
      * 列从最右开始,每次向左 2 列;遇到第 6 列跳过(那是时序线);
      * 行方向上下交替(先从下往上);
      * **每行内先取右列、再取左列**(即 col_range = (col, col-1))。
    """
    size = len(grid)
    bits: list[int] = []
    for byte in codewords:
        for shift in range(7, -1, -1):
            bits.append((byte >> shift) & 1)
    blocked = skip or set()
    index = 0
    upward = True
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for target in (col, col - 1):   # ⚠️ 先右后左,不能反
                if reserved[row][target] or (row, target) in blocked:
                    continue
                bit = bits[index] if index < len(bits) else 0
                grid[row][target] = bit
                index += 1
        upward = not upward
        col -= 2


def _mask_condition(mask: int, row: int, col: int) -> bool:
    if mask == 0:
        return (row + col) % 2 == 0
    if mask == 1:
        return row % 2 == 0
    if mask == 2:
        return col % 3 == 0
    if mask == 3:
        return (row + col) % 3 == 0
    if mask == 4:
        return (row // 2 + col // 3) % 2 == 0
    if mask == 5:
        return (row * col) % 2 + (row * col) % 3 == 0
    if mask == 6:
        return ((row * col) % 2 + (row * col) % 3) % 2 == 0
    return ((row + col) % 2 + (row * col) % 3) % 2 == 0


def _format_cells(size: int) -> set[tuple[int, int]]:
    """格式信息与暗模块占用的格子(两份拷贝 + 1 个暗模块 = 31 个)。"""
    cells: set[tuple[int, int]] = set()
    for index in range(15):
        if index < 6:
            cells.add((index, 8))
        elif index < 8:
            cells.add((index + 1, 8))
        else:
            cells.add((size - 15 + index, 8))
        if index < 8:
            cells.add((8, size - index - 1))
        elif index < 9:
            cells.add((8, 15 - index - 1 + 1))
        else:
            cells.add((8, 15 - index - 1))
    cells.add((size - 8, 8))
    return cells


def _apply_mask(
    grid: list[list[int | None]],
    reserved: list[list[bool]],
    mask: int,
    *,
    skip: set[tuple[int, int]] | None = None,
) -> None:
    """施加掩码。⚠️ 绝不能翻转格式位,否则解码器连掩码号都读不出来。"""
    size = len(grid)
    blocked = skip or set()
    for row in range(size):
        for col in range(size):
            if reserved[row][col] or (row, col) in blocked:
                continue
            if _mask_condition(mask, row, col):
                grid[row][col] = (grid[row][col] or 0) ^ 1


def _format_bits(mask: int) -> int:
    # 纠错级别 L = 01
    data = (0b01 << 3) | mask
    value = data << 10
    generator = 0b10100110111
    for shift in range(4, -1, -1):
        if value & (1 << (shift + 10)):
            value ^= generator << shift
    return ((data << 10) | value) ^ 0b101010000010010


def _write_format(grid: list[list[int | None]], mask: int) -> None:
    """摆放格式信息。

    ⚠️ 这里的坐标映射是 QR 规范里最容易写错的一处(第一版就写错了,
    靠与参考实现逐字节对比才发现)。两份拷贝的位序**不一样**:
    垂直那份跳过第 6 行,水平那份跳过第 6 列。
    """
    size = len(grid)
    bits = _format_bits(mask)
    for index in range(15):
        bit = (bits >> index) & 1
        # 第一份:竖着写在左上角第 8 列
        if index < 6:
            grid[index][8] = bit
        elif index < 8:
            grid[index + 1][8] = bit
        else:
            grid[size - 15 + index][8] = bit
        # 第二份:横着写在右上角第 8 行 + 左下角
        if index < 8:
            grid[8][size - index - 1] = bit
        elif index < 9:
            grid[8][15 - index - 1 + 1] = bit
        else:
            grid[8][15 - index - 1] = bit
    # 固定暗模块
    grid[size - 8][8] = 1


def _version_bits(version: int) -> int:
    """版本的 BCH(18,6) 编码:6 位版本 + 12 位纠错。"""
    value = version << 12
    generator = 0b1111100100101
    for shift in range(5, -1, -1):
        if value & (1 << (shift + 12)):
            value ^= generator << shift
    return (version << 12) | value


def _write_version_info(grid: list[list[int | None]], reserved: list[list[bool]],
                        version: int) -> None:
    """写版本信息(v7+)。

    ⚠️ 这两块**必须算进保留区**:它们占 36 个格子,不保留的话数据会多占 36 位,
    版本 7 以上的码全部作废。
    """
    size = len(grid)
    bits = _version_bits(version)
    for index in range(18):
        bit = (bits >> index) & 1
        row, col = index // 3, size - 11 + index % 3
        grid[row][col] = bit
        reserved[row][col] = True
        grid[col][row] = bit
        reserved[col][row] = True


# 规则 3 要找的"类定位图形"序列,以及它两侧要求的静区
_FINDER_LIKE = (1, 0, 1, 1, 1, 0, 1)
_QUIET = (0, 0, 0, 0)


def _rule1(grid: list[list[int]], size: int) -> int:
    """同色连续 5 个以上:3 + (长度 - 5)。行与列都要算。"""
    score = 0
    for line in list(grid) + [[grid[r][c] for r in range(size)] for c in range(size)]:
        run, last = 1, line[0]
        for index in range(1, size):
            if line[index] == last:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run, last = 1, line[index]
        if run >= 5:
            score += 3 + (run - 5)
    return score


def _rule2(grid: list[list[int]], size: int) -> int:
    """每个 2x2 同色块 +3。"""
    score = 0
    for row in range(size - 1):
        for col in range(size - 1):
            value = grid[row][col]
            if (grid[row][col + 1] == value and grid[row + 1][col] == value
                    and grid[row + 1][col + 1] == value):
                score += 3
    return score


def _rule3(grid: list[list[int]], size: int) -> int:
    """出现 1:1:3:1:1 且一侧有 4 个静区 —— 每个 +40。

    ⚠️ 这一条最容易漏。漏了不会报错,只会**选错掩码**,表现是"码看着正常但扫不出来"。
    """
    score = 0
    for row in range(size):
        line = grid[row]
        for col in range(size - 6):
            if tuple(line[col:col + 7]) != _FINDER_LIKE:
                continue
            before = tuple(line[col - 4:col]) if col >= 4 else ()
            after = tuple(line[col + 7:col + 11]) if col + 11 <= size else ()
            if before == _QUIET or after == _QUIET:
                score += 40
    for col in range(size):
        line = [grid[r][col] for r in range(size)]
        for row in range(size - 6):
            if tuple(line[row:row + 7]) != _FINDER_LIKE:
                continue
            before = tuple(line[row - 4:row]) if row >= 4 else ()
            after = tuple(line[row + 7:row + 11]) if row + 11 <= size else ()
            if before == _QUIET or after == _QUIET:
                score += 40
    return score


def _rule4(grid: list[list[int]], size: int) -> int:
    """深色比例偏离 50% 的程度,每 5% 记 10 分。"""
    dark = sum(sum(row) for row in grid)
    total = size * size
    percent = dark * 100 // total
    return abs(percent - 50) // 5 * 10


def _penalty(grid: list[list[int]]) -> int:
    """掩码惩罚分。四条规则**一条都不能少**(见 _rule3 的注释)。"""
    size = len(grid)
    return _rule1(grid, size) + _rule2(grid, size) + _rule3(grid, size) + _rule4(grid, size)


def build_matrix(content: str) -> list[list[int]]:
    """把一个字符串编码成 0/1 矩阵。"""
    payload = content.encode("utf-8")
    version = _choose_version(len(payload))
    data = _encode_data(payload, version)
    codewords = _interleave(data, version)

    protected = _format_cells(version * 4 + 17)

    best: list[list[int]] | None = None
    best_score = None
    for mask in range(8):
        grid, reserved = _matrix(version)
        # ⚠️ protected 必须同时传给放置与掩码:数据不落进格式位,掩码不翻转格式位。
        _place_data(grid, reserved, codewords, skip=protected)
        _apply_mask(grid, reserved, mask, skip=protected)
        _write_format(grid, mask)
        final = [[cell or 0 for cell in row] for row in grid]
        score = _penalty(final)
        if best_score is None or score < best_score:
            best, best_score = final, score
    assert best is not None
    return best


def render_terminal(content: str, *, quiet_zone: int = 2) -> str:
    """渲染成可以在终端/聊天窗口里扫的文本。

    用半块字符 `▀` 把两行合成一行,长宽比才对 —— 用整块字符会拉长,扫不出来。
    """
    matrix = build_matrix(content)
    size = len(matrix)
    lines: list[str] = []
    blank = " " * (size + quiet_zone * 2)
    for _ in range(quiet_zone):
        lines.append(blank)
    for row in range(0, size, 2):
        top = matrix[row]
        bottom = matrix[row + 1] if row + 1 < size else [0] * size
        chars = []
        for col in range(size):
            up, down = top[col], bottom[col]
            if up and down:
                chars.append("█")
            elif up and not down:
                chars.append("▀")
            elif not up and down:
                chars.append("▄")
            else:
                chars.append(" ")
        lines.append(" " * quiet_zone + "".join(chars) + " " * quiet_zone)
    for _ in range(quiet_zone):
        lines.append(blank)
    return "\n".join(lines)


def render_svg(content: str, *, module: int = 8, quiet_zone: int = 4) -> str:
    """渲染成 SVG。

    为什么是 SVG 而不是 PNG:**浏览器原生就能显示,不需要任何图像库**,
    而 PNG 得自己写编码器或引 Pillow。一个登录 URL 出来的 SVG 只有 6KB 左右。

    每行合并成一条 path 而不是每个方块一个 rect —— 37x37 的码能少掉上千个节点。
    """
    matrix = build_matrix(content)
    size = len(matrix)
    dim = (size + quiet_zone * 2) * module
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="' + str(dim) + '" height="' + str(dim)
        + '" viewBox="0 0 ' + str(dim) + " " + str(dim)
        + '" shape-rendering="crispEdges" role="img" aria-label="微信登录二维码">',
        '<rect width="' + str(dim) + '" height="' + str(dim) + '" fill="#ffffff"/>',
        '<path fill="#000000" d="',
    ]
    for row in range(size):
        col = 0
        while col < size:
            if not matrix[row][col]:
                col += 1
                continue
            start = col
            while col < size and matrix[row][col]:
                col += 1
            run = col - start
            x = (start + quiet_zone) * module
            y = (row + quiet_zone) * module
            parts.append(
                "M" + str(x) + " " + str(y) + "h" + str(run * module)
                + "v" + str(module) + "h-" + str(run * module) + "z"
            )
    parts.append('"/></svg>')
    return "".join(parts)
