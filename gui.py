"""Small desktop GUI for KIS analysis and explicitly confirmed orders.

Run with ``.venv/bin/python gui.py``.  Tkinter is part of Python on macOS and
Windows, so this intentionally adds no GUI dependency.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk
from zoneinfo import ZoneInfo


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("TradingAgents · 한국투자증권")
        root.geometry("900x650")
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.ticker = tk.StringVar(value="005930.KS")
        self.date = tk.StringVar(value=datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d"))
        self.side = tk.StringVar(value="buy")
        self.order_type = tk.StringVar(value="limit")
        self.quantity = tk.StringVar(value="1")
        self.price = tk.StringVar(value="")
        self.request_id = tk.StringVar(value="")
        self.execute = tk.BooleanVar(value=False)
        self._build()
        root.after(100, self._drain)

    def _build(self):
        pad = {"padx": 8, "pady": 5}
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)
        fields = ttk.LabelFrame(main, text="분석")
        fields.pack(fill="x")
        ttk.Label(fields, text="종목코드").grid(row=0, column=0, **pad)
        ttk.Entry(fields, textvariable=self.ticker, width=18).grid(row=0, column=1, **pad)
        ttk.Label(fields, text="기준일 (YYYY-MM-DD)").grid(row=0, column=2, **pad)
        ttk.Entry(fields, textvariable=self.date, width=14).grid(row=0, column=3, **pad)
        ttk.Button(fields, text="분석 실행", command=self.analyze).grid(row=0, column=4, **pad)

        order = ttk.LabelFrame(main, text="주문")
        order.pack(fill="x", pady=10)
        ttk.Label(order, text="방향").grid(row=0, column=0, **pad)
        ttk.Combobox(order, textvariable=self.side, values=("buy", "sell"), state="readonly", width=8).grid(row=0, column=1, **pad)
        ttk.Label(order, text="수량").grid(row=0, column=2, **pad)
        ttk.Entry(order, textvariable=self.quantity, width=8).grid(row=0, column=3, **pad)
        ttk.Label(order, text="유형").grid(row=0, column=4, **pad)
        ttk.Combobox(order, textvariable=self.order_type, values=("limit", "market"), state="readonly", width=8).grid(row=0, column=5, **pad)
        ttk.Label(order, text="가격(KRW)").grid(row=0, column=6, **pad)
        ttk.Entry(order, textvariable=self.price, width=10).grid(row=0, column=7, **pad)
        ttk.Label(order, text="요청 ID").grid(row=1, column=0, **pad)
        ttk.Entry(order, textvariable=self.request_id, width=22).grid(row=1, column=1, columnspan=2, **pad)
        ttk.Checkbutton(order, text="실전 주문 전송 (--execute)", variable=self.execute).grid(row=1, column=3, columnspan=3, **pad)
        ttk.Button(order, text="주문 미리보기 / 전송", command=self.place_order).grid(row=1, column=6, columnspan=2, **pad)
        ttk.Label(order, text="실전 전송은 확인 창을 한 번 더 표시합니다.", foreground="#9b1c1c").grid(row=2, column=0, columnspan=8, sticky="w", **pad)

        self.output = tk.Text(main, wrap="word", state="disabled", font=("Menlo", 11))
        self.output.pack(fill="both", expand=True)
        self.status = ttk.Label(main, text="준비됨")
        self.status.pack(anchor="w")

    def _write(self, text: str):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("end", text)
        self.output.configure(state="disabled")

    def _run(self, fn):
        self.status.configure(text="처리 중…")
        threading.Thread(target=lambda: self._worker(fn), daemon=True).start()

    def _worker(self, fn):
        try:
            self.events.put(("ok", fn()))
        except Exception as exc:  # UI must remain usable after API errors.
            self.events.put(("error", f"{type(exc).__name__}: {exc}"))

    def _drain(self):
        try:
            kind, value = self.events.get_nowait()
        except queue.Empty:
            self.root.after(100, self._drain)
            return
        self._write(str(value))
        self.status.configure(text="완료" if kind == "ok" else "오류")
        self.root.after(100, self._drain)

    def analyze(self):
        ticker, date = self.ticker.get().strip().upper(), self.date.get().strip()
        self._run(lambda: __import__("tradingagents.graph.trading_graph", fromlist=["TradingAgentsGraph"]).TradingAgentsGraph(
            debug=True, config=__import__("tradingagents.default_config", fromlist=["korean_config"]).korean_config()
        ).propagate(ticker, date)[1])

    def place_order(self):
        from tradingagents.brokers.kis import KISBroker

        try:
            quantity, price = int(self.quantity.get()), int(self.price.get() or 0)
            request_id = self.request_id.get().strip() or datetime.now().strftime("gui-%Y%m%d%H%M%S")
            execute = self.execute.get()
            preview = KISBroker().preview(self.ticker.get().strip(), self.side.get(), quantity, price, self.order_type.get())
            if execute and not messagebox.askyesno("실전 주문 확인", f"다음 주문을 실제 전송합니다.\n\n{preview}\n\n계속하시겠습니까?"):
                return
            self._run(lambda: KISBroker().place_order(self.ticker.get().strip(), self.side.get(), quantity, price,
                                                      self.order_type.get(), request_id=request_id, execute=execute))
        except Exception as exc:
            messagebox.showerror("주문 입력 오류", str(exc))


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
