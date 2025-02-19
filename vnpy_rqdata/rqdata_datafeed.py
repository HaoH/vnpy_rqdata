from datetime import datetime, timedelta, date
from typing import Dict, List, Set, Optional, Callable

import pandas as pd
from numpy import ndarray
from pandas import DataFrame, Timestamp
from rqdatac import init, index_components, get_ex_factor, get_shares
from rqdatac.services.get_price import get_price
from rqdatac.services.future import get_dominant_price
from rqdatac.services.basic import all_instruments
from rqdatac.share.errors import RQDataError

from ex_vnpy.object import SharesData
from vnpy.trader.setting import SETTINGS
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData, TickData, HistoryRequest
from vnpy.trader.utility import round_to, ZoneInfo
from vnpy.trader.datafeed import BaseDatafeed


INTERVAL_VT2RQ: Dict[Interval, str] = {
    Interval.MINUTE: "1m",
    Interval.HOUR: "60m",
    Interval.DAILY: "1d",
    Interval.WEEKLY: "1w"
}

INTERVAL_ADJUSTMENT_MAP: Dict[Interval, timedelta] = {
    Interval.MINUTE: timedelta(minutes=1),
    Interval.HOUR: timedelta(hours=1),
    Interval.DAILY: timedelta(),        # no need to adjust for daily bar
    Interval.WEEKLY: timedelta()        # no need to adjust for daily bar
}

FUTURES_EXCHANGES: Set[Exchange] = {
    Exchange.CFFEX,
    Exchange.SHFE,
    Exchange.CZCE,
    Exchange.DCE,
    Exchange.INE,
    Exchange.GFEX
}

CHINA_TZ = ZoneInfo("Asia/Shanghai")


def to_rq_symbol(symbol: str, exchange: Exchange, all_symbols: ndarray) -> str:
    """将交易所代码转换为米筐代码"""
    # 股票
    if exchange in {Exchange.SSE, Exchange.SZSE}:
        if exchange == Exchange.SSE:
            rq_symbol: str = f"{symbol}.XSHG"
        else:
            rq_symbol: str = f"{symbol}.XSHE"
    # 金交所现货
    elif exchange == Exchange.SGE:
        for char in ["(", ")", "+"]:
            symbol: str = symbol.replace(char, "")
        symbol = symbol.upper()
        rq_symbol: str = f"{symbol}.SGEX"
    # 期货和期权
    elif exchange in {
        Exchange.CFFEX,
        Exchange.SHFE,
        Exchange.DCE,
        Exchange.CZCE,
        Exchange.INE,
        Exchange.GFEX
    }:
        for count, word in enumerate(symbol):
            if word.isdigit():
                break

        product: str = symbol[:count]
        time_str: str = symbol[count:]

        # 期货
        if time_str.isdigit():
            # 只有郑商所需要特殊处理
            if exchange is not Exchange.CZCE:
                return symbol.upper()

            # 检查是否为连续合约或者指数合约
            if time_str in ["88", "888", "99", "889"]:
                return symbol

            # 提取年月
            year: str = symbol[count]
            month: str = symbol[count + 1:]

            guess_1: str = f"{product}1{year}{month}".upper()
            guess_2: str = f"{product}2{year}{month}".upper()

            # 优先尝试20年后的合约
            if guess_2 in all_symbols:
                rq_symbol: str = guess_2
            else:
                rq_symbol: str = guess_1
        # 期权以及期货次主力连续合约
        else:
            if time_str == "88A2":
                return symbol

            if exchange in {
                Exchange.CFFEX,
                Exchange.DCE,
                Exchange.SHFE,
                Exchange.INE,
                Exchange.GFEX
            }:
                rq_symbol: str = symbol.replace("-", "").upper()
            elif exchange == Exchange.CZCE:
                year: str = symbol[count]
                suffix: str = symbol[count + 1:]

                guess_1: str = f"{product}1{year}{suffix}".upper()
                guess_2: str = f"{product}2{year}{suffix}".upper()

                # 优先尝试20年后的合约
                if guess_2 in all_symbols:
                    rq_symbol: str = guess_2
                else:
                    rq_symbol: str = guess_1
    else:
        rq_symbol: str = f"{symbol}.{exchange.value}"

    return rq_symbol

def to_rq_symbol_from_vt(vt_symbol: str, all_symbols: ndarray) -> str:
    """将交易所代码转换为米筐代码"""
    # 股票
    symbol, exchange = vt_symbol.split('.')
    exchange = Exchange(exchange)
    return to_rq_symbol(symbol, exchange, all_symbols)

class RqdataDatafeed(BaseDatafeed):
    """米筐RQData数据服务接口"""

    def __init__(self, username=None, password=None):
        """"""
        self.username: str = SETTINGS["datafeed.username"] if username is None else username
        self.password: str = SETTINGS["datafeed.password"] if password is None else password

        self.inited: bool = False
        self.symbols: ndarray = None

    def init(self, output: Callable = print) -> bool:
        """初始化"""
        if self.inited:
            return True

        if not self.username:
            output("RQData数据服务初始化失败：用户名为空！")
            return False

        if not self.password:
            output("RQData数据服务初始化失败：密码为空！")
            return False

        try:
            init(
                self.username,
                self.password,
                ("rqdatad-pro.ricequant.com", 16011),
                use_pool=True,
                max_pool_size=1,
                auto_load_plugins=False
            )

            df: DataFrame = all_instruments()
            self.symbols = df["order_book_id"].values
        except RQDataError as ex:
            output(f"RQData数据服务初始化失败：{ex}")
            return False
        except RuntimeError as ex:
            output(f"发生运行时错误：{ex}")
            return False
        except Exception as ex:
            output(f"发生未知异常：{ex}")
            return False

        self.inited = True
        return True

    def query_bar_history(self, req: HistoryRequest, output: Callable = print) -> Optional[List[BarData]]:
        """查询K线数据"""
        # 期货品种且代码中没有数字（非具体合约），则查询主力连续
        if req.exchange in FUTURES_EXCHANGES and req.symbol.isalpha():
            return self._query_dominant_history(req, output)
        else:
            return self._query_bar_history(req, output)

    def _query_bar_history(self, req: HistoryRequest, output: Callable = print) -> Optional[List[BarData]]:
        """查询K线数据"""
        if not self.inited:
            n: bool = self.init(output)
            if not n:
                return []

        symbol: str = req.symbol
        exchange: Exchange = req.exchange
        interval: Interval = req.interval
        start: datetime = req.start
        end: datetime = req.end

        # 股票期权不添加交易所后缀
        if exchange in [Exchange.SSE, Exchange.SZSE] and symbol in self.symbols:
            rq_symbol: str = symbol
        else:
            rq_symbol: str = to_rq_symbol(symbol, exchange, self.symbols)

        # 检查查询的代码在范围内
        if rq_symbol not in self.symbols:
            output(f"RQData查询K线数据失败：不支持的合约代码{req.vt_symbol}")
            return []

        rq_interval: str = INTERVAL_VT2RQ.get(interval)
        if not rq_interval:
            output(f"RQData查询K线数据失败：不支持的时间周期{req.interval.value}")
            return []

        # 为了将米筐时间戳（K线结束时点）转换为VeighNa时间戳（K线开始时点）
        adjustment: timedelta = INTERVAL_ADJUSTMENT_MAP[interval]

        # 为了查询夜盘数据
        end += timedelta(1)

        # 只对衍生品合约才查询持仓量数据
        fields: list = ["open", "high", "low", "close", "volume", "total_turnover"]
        if not symbol.isdigit():
            fields.append("open_interest")

        df: DataFrame = get_price(
            rq_symbol,
            frequency=rq_interval,
            fields=fields,
            start_date=start,
            end_date=end
            # adjust_type="none"
        )

        data: List[BarData] = []

        if df is not None:
            # 填充NaN为0
            df.fillna(0, inplace=True)

            for row in df.itertuples():
                if type(row.Index[1]) == Timestamp:
                    dt: datetime = row.Index[1].to_pydatetime() - adjustment
                elif type(row.Index[1]) == date:
                    dt: datetime = row.Index[1] - adjustment
                    dt = datetime.combine(dt.today(), datetime.min.time())

                dt: datetime = dt.replace(tzinfo=CHINA_TZ)

                bar: BarData = BarData(
                    symbol=symbol,
                    exchange=exchange,
                    interval=interval,
                    datetime=dt,
                    open_price=round_to(row.open, 0.000001),
                    high_price=round_to(row.high, 0.000001),
                    low_price=round_to(row.low, 0.000001),
                    close_price=round_to(row.close, 0.000001),
                    volume=row.volume,
                    turnover=row.total_turnover,
                    open_interest=getattr(row, "open_interest", 0),
                    gateway_name="RQ"
                )

                data.append(bar)

        return data

    def query_tick_history(self, req: HistoryRequest, output: Callable = print) -> Optional[List[TickData]]:
        """查询Tick数据"""
        if not self.inited:
            n: bool = self.init(output)
            if not n:
                return []

        symbol: str = req.symbol
        exchange: Exchange = req.exchange
        start: datetime = req.start
        end: datetime = req.end

        # 股票期权不添加交易所后缀
        if exchange in [Exchange.SSE, Exchange.SZSE] and symbol in self.symbols:
            rq_symbol: str = symbol
        else:
            rq_symbol: str = to_rq_symbol(symbol, exchange, self.symbols)

        if rq_symbol not in self.symbols:
            output(f"RQData查询Tick数据失败：不支持的合约代码{req.vt_symbol}")
            return []

        # 为了查询夜盘数据
        end += timedelta(1)

        # 只对衍生品合约才查询持仓量数据
        fields: list = [
            "open",
            "high",
            "low",
            "last",
            "prev_close",
            "volume",
            "total_turnover",
            "limit_up",
            "limit_down",
            "b1",
            "b2",
            "b3",
            "b4",
            "b5",
            "a1",
            "a2",
            "a3",
            "a4",
            "a5",
            "b1_v",
            "b2_v",
            "b3_v",
            "b4_v",
            "b5_v",
            "a1_v",
            "a2_v",
            "a3_v",
            "a4_v",
            "a5_v",
        ]
        if not symbol.isdigit():
            fields.append("open_interest")

        df: DataFrame = get_price(
            rq_symbol,
            frequency="tick",
            fields=fields,
            start_date=start,
            end_date=end
            # adjust_type="none"
        )

        data: List[TickData] = []

        if df is not None:
            # 填充NaN为0
            df.fillna(0, inplace=True)

            for row in df.itertuples():
                dt: datetime = row.Index[1].to_pydatetime()
                dt: datetime = dt.replace(tzinfo=CHINA_TZ)

                tick: TickData = TickData(
                    symbol=symbol,
                    exchange=exchange,
                    datetime=dt,
                    open_price=row.open,
                    high_price=row.high,
                    low_price=row.low,
                    pre_close=row.prev_close,
                    last_price=row.last,
                    volume=row.volume,
                    turnover=row.total_turnover,
                    open_interest=getattr(row, "open_interest", 0),
                    limit_up=row.limit_up,
                    limit_down=row.limit_down,
                    bid_price_1=row.b1,
                    bid_price_2=row.b2,
                    bid_price_3=row.b3,
                    bid_price_4=row.b4,
                    bid_price_5=row.b5,
                    ask_price_1=row.a1,
                    ask_price_2=row.a2,
                    ask_price_3=row.a3,
                    ask_price_4=row.a4,
                    ask_price_5=row.a5,
                    bid_volume_1=row.b1_v,
                    bid_volume_2=row.b2_v,
                    bid_volume_3=row.b3_v,
                    bid_volume_4=row.b4_v,
                    bid_volume_5=row.b5_v,
                    ask_volume_1=row.a1_v,
                    ask_volume_2=row.a2_v,
                    ask_volume_3=row.a3_v,
                    ask_volume_4=row.a4_v,
                    ask_volume_5=row.a5_v,
                    gateway_name="RQ"
                )

                data.append(tick)

        return data

    def _query_dominant_history(self, req: HistoryRequest, output: Callable = print) -> Optional[List[BarData]]:
        """查询期货主力K线数据"""
        if not self.inited:
            n: bool = self.init(output)
            if not n:
                return []

        symbol: str = req.symbol
        exchange: Exchange = req.exchange
        interval: Interval = req.interval
        start: datetime = req.start
        end: datetime = req.end

        rq_interval: str = INTERVAL_VT2RQ.get(interval)
        if not rq_interval:
            output(f"RQData查询K线数据失败：不支持的时间周期{req.interval.value}")
            return []

        # 为了将米筐时间戳（K线结束时点）转换为VeighNa时间戳（K线开始时点）
        adjustment: timedelta = INTERVAL_ADJUSTMENT_MAP[interval]

        # 为了查询夜盘数据
        end += timedelta(1)

        # 只对衍生品合约才查询持仓量数据
        fields: list = ["open", "high", "low", "close", "volume", "total_turnover"]
        if not symbol.isdigit():
            fields.append("open_interest")

        df: DataFrame = get_dominant_price(
            symbol.upper(),                         # 合约代码用大写
            frequency=rq_interval,
            fields=fields,
            start_date=start,
            end_date=end,
            adjust_type="pre",                      # 前复权
            adjust_method="prev_close_ratio"        # 切换前一日收盘价比例复权
        )

        data: List[BarData] = []

        if df is not None:
            # 填充NaN为0
            df.fillna(0, inplace=True)

            for row in df.itertuples():
                dt: datetime = row.Index[1].to_pydatetime() - adjustment
                dt: datetime = dt.replace(tzinfo=CHINA_TZ)

                bar: BarData = BarData(
                    symbol=symbol,
                    exchange=exchange,
                    interval=interval,
                    datetime=dt,
                    open_price=round_to(row.open, 0.000001),
                    high_price=round_to(row.high, 0.000001),
                    low_price=round_to(row.low, 0.000001),
                    close_price=round_to(row.close, 0.000001),
                    volume=row.volume,
                    turnover=row.total_turnover,
                    open_interest=getattr(row, "open_interest", 0),
                    gateway_name="RQ"
                )

                data.append(bar)

        return data

    def index_components(self, symbol,  output: Callable = print):
        """
        Query index components
        """
        if not self.inited:
            n: bool = self.init(output)
            if not n:
                return None

        return index_components(symbol)

    def get_ex_factor(self, symbols, output: Callable = print):
        """
        Query ex_factor
        """
        if not self.inited:
            n: bool = self.init(output)
            if not n:
                return None

        return get_ex_factor(symbols)

    def get_shares(self, symbols, start_date, end_date, output: Callable = print):
        """
        Query shares
        """
        if not self.inited:
            n: bool = self.init(output)
            if not n:
                return None

        return get_shares(symbols, start_date, end_date)

    def query_shares_history(self, vt_symbols: List[str], start: datetime, end: datetime, output: Callable = print):
        if not self.inited:
            n: bool = self.init(output)
            if not n:
                return []

        # 默认多取一天的数据，用来判断start日期是否发生变更
        # 如果结果的第一条日期晚于start，则需要保留
        new_start: datetime = start - timedelta(days=1)
        start_str = new_start.strftime("%Y-%m-%d")
        end_str = end.strftime("%Y-%m-%d")
        rq_symbols = [to_rq_symbol_from_vt(vt_symbol, self.symbols) for vt_symbol in vt_symbols]
        shares_df = get_shares(rq_symbols, start_str, end_str, expect_df=True)
        shares_df.sort_values(by=['order_book_id', 'date'], inplace=True)

        # 对circulation_a值为NaN的情况进行填充，根据下一个非空的 circulation_a/total_a 的比值缩放当前的circulation_a
        # 先对total_a使用后续第一个非NaN的值进行填充, e.g. 600938
        shares_df['total_a'] = shares_df['total_a'].fillna(method='bfill')

        # e.g. 000166
        shares_df['next_valid_ratio'] = (shares_df['circulation_a'] / shares_df['total_a']).fillna(method='bfill')
        shares_df['circulation_a'] = shares_df.apply(
            lambda row: row['total_a'] * row['next_valid_ratio'] if pd.isna(row['circulation_a']) else row['circulation_a'],
            axis=1
        )

        # 计算变化
        shares_df['previous'] = shares_df.groupby('order_book_id')['circulation_a'].shift(1)
        shares_df['change'] = shares_df['circulation_a'] - shares_df['previous']
        changed_df = shares_df[(shares_df['change'] != 0)].reset_index()

        # 将单一变化时点转换为开始和结束日期，方便联合查询
        changed_df['start_dt'] = changed_df['date']
        changed_df['end_dt'] = changed_df.groupby('order_book_id')['date'].shift(-1) - pd.Timedelta(days=1)
        changed_df['end_dt'] = changed_df['end_dt'].transform(lambda x: '9999-12-31' if pd.isna(x) else x)

        # 假设：超过30天的查询表示数据库重建, 低于30天的查询表示每天的数据更新
        if end - new_start < timedelta(days=30):
            first_date_str = shares_df.index[0][1].strftime('%Y-%m-%d') if len(shares_df) > 0 else start_str
            changed_df = changed_df[changed_df['date'] != first_date_str]

        new_df = changed_df[['order_book_id', 'start_dt', 'end_dt', 'circulation_a', 'non_circulation_a', 'total_a', 'total']].copy()
        new_df['symbol'] = new_df['order_book_id'].transform(lambda x: x[:6])
        new_df['exchange'] = new_df['order_book_id'].transform(lambda x: 'SSE' if x[-4:] == 'XSHG' else 'SZSE')

        new_change_start: List = new_df.groupby('order_book_id')[['symbol', 'exchange', 'start_dt']].first().to_dict(
            'records')

        results: List[SharesData] = SharesData.from_dicts(new_df.to_dict('records'))
        return results, new_change_start
