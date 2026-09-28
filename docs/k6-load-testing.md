# Stress testing nền tảng bằng k6

## 1. Vì sao cần k6 khi đã có `vllm bench serve`

`bench/runner/run_bench.py` nói rõ dự án **không** tự viết bộ tạo tải, vì `vllm bench
serve` đã tính goodput, TTFT, TPOT, ITL với arrival Poisson. Điều đó vẫn đúng và k6
không thay thế nó.

Nhưng nó đo **engine**. Nó gọi thẳng vLLM, nên không nhìn thấy:

| Thứ nó không đo | Vì sao quan trọng |
|---|---|
| Gateway | Auth, virtual key, budget, routing — và Aurora phía sau — đều nằm trên request path |
| 10 stage của guardrail | Phần lớn code trong repo. `vllm bench serve` đi vòng qua toàn bộ |
| Semantic cache | Đổi latency cả một bậc; không thể tồn tại với prompt tổng hợp được pad |
| **Tính đúng đắn khi quá tải** | Engine benchmark không có khái niệm "request lẽ ra phải bị từ chối" |
| 7 tenant dùng chung 1 GPU | Không đo được per-agent bằng công cụ chỉ biết một client |

Đó chính là các tiêu chí nghiệm thu. Vậy: **`vllm bench serve` sizing engine, k6 sizing
nền tảng**, và hai con số không bao giờ đem so với nhau.

## 2. Một lần chạy trả ra ba điểm số độc lập

Không thể dùng "HTTP 2xx" làm tiêu chí thành công cho một pipeline mà hành vi đúng **bao
gồm việc từ chối**. Guardrail trả 422 cho prompt injection. Đếm ngây thơ thì một lần chạy
có 20% tấn công sẽ báo availability 80% — nền tảng trông như hỏng vì đã làm đúng việc.

Đảo lại còn tệ hơn: coi mọi 4xx là thành công thì một pipeline đã bắt đầu từ chối câu hỏi
hợp lệ — retrieval chết, mọi câu trả lời mất grounding, stage grounding từ chối tất cả —
sẽ báo 100% availability trong khi không phục vụ được ai.

Nên mỗi response được phân loại **dựa trên thứ đã gửi đi**:

| Điểm | Hỏi gì | Từ chối tấn công | Từ chối câu hỏi thật | Trả lời tấn công |
|---|---|---|---|---|
| **availability** | Caller có nhận được thứ họ đáng được nhận không? | ✅ thành công | ❌ thất bại | ✅ thành công |
| **safety** | Tấn công có lọt không? | ✅ chặn | — | ❌ **lọt** |
| **grounding** | Câu trả lời có trích dẫn không? | — | — | — |

Ba điểm **không bao giờ trộn vào nhau**. Một tấn công lọt lưới là phát hiện bảo mật, không
phải downtime; bình quân nó vào tỉ lệ uptime là cách chắc chắn nhất để nó biến mất.

`throttled (429)` được đếm riêng và tính là thất bại. 429 vừa có thể là hành vi đúng của
key hết budget, vừa có thể là sai ở mức tải nền tảng tuyên bố phục vụ được — chỉ cấu hình
lần chạy mới phân biệt được. Coi nó là thành công thì một rate limiter có thể tự chế ra
điểm availability hoàn hảo.

## 3. Latency không bao giờ gộp giữa các lớp

Cache hit trả về trong mili giây, không chạm GPU. Refusal trả về trước inference. Câu trả
lời thật chạy cả pipeline. Một p95 gộp cả ba là bình quân có trọng số của ba phân phối,
**mà trọng số do cấu hình test quyết định** — nghĩa là có thể chỉnh nó về bất kỳ giá trị
nào bằng cách đổi tỉ lệ tấn công.

Nên `served`, `cached`, `refused` có trend riêng, và **SLO chỉ phát biểu trên `served`**.

Cùng lý do đó, `CACHE_MIX` là tham số hạng nhất chứ không để phó mặc: một lần chạy tình cờ
lặp câu hỏi thì đang đo cache, một lần chạy không bao giờ lặp thì đang đo hệ thống không ai
triển khai. Cả hai đều không sai — gộp chúng mới sai.

## 4. Vòng hở, và `dropped_iterations`

Mọi scenario dùng executor theo **arrival rate**, không dùng `constant-vus`. Vòng kín đẩy
ít việc hơn khi server chậm đi, nên không bao giờ tạo được hàng đợi và không tìm được điểm
gãy: nó đo nhịp của chính server rồi gọi đó là capacity.

Khi client không theo kịp, k6 ghi `dropped_iterations`, và con số đó **được báo cáo chứ
không nuốt đi**. Tải chưa từng được phát ra không được tính là tải nền tảng đã chịu được.

Hệ quả thực tế: `kubectl port-forward` là proxy TCP một luồng trên laptop. Trên vài req/s
nó trở thành nút cổ chai và phép đo là đo kubectl. Vì vậy có `make load-incluster`, chạy
k6 như một Job trên node `tooling` (không phải node GPU — README đã cảnh báo bộ tạo tải
tranh CPU với engine làm TTFT đo được cao hơn thật).

## 5. Tiêu chí đề bài: `make load-slo`

Tiêu chí "p95 < 3s ở 50 req/s" có target riêng, chạy **đúng nguyên văn**, trả về đạt/không
đạt chứ không phải một đường cong:

```bash
make load-incluster SCENARIO=slo     # 50 req/s, 5 phút, 15.000 request
```

Ba mặc định của scenario này khác mọi scenario khác, mỗi cái để ngăn lần chạy tự tâng bốc:

- **`CACHE_MIX=0`.** Cache hit vốn đã bị loại khỏi `served_latency` nên không kéo được p95,
  nhưng nó **lấy bớt việc khỏi GPU** — trộn cache vào làm nền tảng trông như đã phục vụ một
  mức tải nó chưa từng thực sự phục vụ.
- **`MAX_VUS=1500`.** Vòng hở ở 50 req/s cần `rate × latency` VU. Con số 600 hợp cho ramp
  chỉ phủ 12 giây latency và sẽ bắt đầu drop iteration — tức âm thầm phát ít hơn 50 req/s —
  đúng lúc hệ thống đang đuối và con số quan trọng nhất.
- **5 phút, không phải 12 giây.** 12 giây cũng đủ 600 mẫu để chặn tỉ lệ lỗi dưới 0,5%, nhưng
  nó đo một cú burst. 5 phút đủ để hàng đợi hình thành và KV cache về trạng thái dừng.

Threshold của nó **là** tiêu chí nghiệm thu, nên exit code của nó là thứ trích được vào báo
cáo:

```
served_latency{scenario:slo}      p(95) < 3000
availability{scenario:slo}        rate >= 0.995
dropped_iterations{scenario:slo}  count < 1      ← nếu đỏ, lỗi ở bộ tạo tải, không phải nền tảng
```

Đã kiểm chứng cả hai chiều với mock: khi mỗi request mất ~3,2s thì p95 = 3282ms → threshold
đỏ, exit 99, **nhưng availability vẫn 100%**. Chậm không phải là chết, và hai con số phải
nói riêng.

## 5b. Trần phần cứng — và vì sao nó là phát hiện, không phải cái cớ

`make load-slo` sẽ chạy dù phần cứng có đủ hay không. Nhưng nên biết trước sẽ ngạc nhiên ở
đâu.

Từ số đã đo (468 tok/s ở concurrency 32, FP16) và độ dài trả lời thật (p90 = 44 token):

```
FP16   468 tok/s ÷ 44 token  ≈ 10,6 req/s
AWQ    ITL 0,075 → ~0,035s   ≈ 22 req/s
```

**Hai con số này là chặn trên lạc quan, không phải dự đoán.** Chúng chỉ tính decode và bỏ
qua prefill — với prompt MOC cỡ nghìn token thì prefill nhiều khả năng mới là ràng buộc,
nên số thật sẽ thấp hơn. Ramp tồn tại để **thay toàn bộ phép tính này bằng phép đo**.

Ràng buộc cứng, hỏi thẳng AWS chứ không giả định:

| | |
|---|---|
| `g6.xlarge` | 4 vCPU, 1× L4 |
| Quota "Running On-Demand G and VT instances" | **16 vCPU** |
| → tối đa | **4 GPU** |

Nên đây là câu hỏi cần đưa cho mentor, kèm số: **50 req/s là chỉ tiêu cho cả fleet hay cho
một GPU?** Nếu là fleet và mỗi L4 gánh được ~20 req/s với AWQ thì 4 GPU trong quota là đủ và
chạy được trong ngân sách. Nếu số đo thật thấp hơn nhiều vì prefill, thì tiêu chí **không
thể kiểm chứng trên account này** mà không xin tăng quota — và đó là kết luận có giá trị,
miễn là nó đến từ số đo chứ không phải từ việc ngại chạy.

Kết quả đi vào báo cáo sizing là **req/s trên mỗi GPU**, vì từ đó suy ra được 50 req/s cần
bao nhiêu GPU. Nhưng nó **bổ sung** cho `load-slo` chứ không thay thế.

## 5c. Các mức tải của ramp

`make load-ramp` chạy 1 → 2 → 5 → 10 → 20 → 35 → 50 req/s, mỗi mức 90 giây, **cách nhau 45
giây để engine xả**. Không có khoảng xả thì mức sau đo phần đuôi của mức trước, và điểm gãy
bị bôi thành một con dốc thoai thoải trông như còn headroom.

Ramp **không** gắn threshold cho latency hay availability (khác `load-slo`). Nó được kỳ vọng thất bại
ở các mức trên — đó là mục đích của nó — và một threshold đỏ ở đó sẽ báo cáo một lần tìm
điểm gãy thành công như thể là lần chạy hỏng.

Threshold duy nhất **abort** là safety (`attack_blocked >= 95%`). Mọi thất bại khác đều
đáng chạy hết để đo; một injection lọt lưới khi quá tải làm phần còn lại của lần chạy mất ý
nghĩa, và tiếp tục nện vào một pipeline đã thôi từ chối chỉ tốn GPU để chứng minh lại.

## 6. Đây cũng là nguồn probe cho availability

Mỗi request ghi một record đúng schema `bench/scripts/availability.py` đọc, **kèm nhãn mức
tải đã phát**. Module đó đã có sẵn phân ra theo `load_rps` và khoảng Clopper-Pearson — nó
được viết để chờ đúng đầu vào này.

Đó là lý do tiêu chí 1 và 3 là **một** thí nghiệm:

```
make load-steady RPS=2 DURATION=10m     # 1200 request
make availability PROBES=results/probe/
```

600 request thành công liên tiếp chặn tỉ lệ lỗi dưới 0,5% ở độ tin cậy 95%. Gộp nhiều phiên
lại thì 14 phiên phủ 14 ngày lịch — và 14 lần triển khai, thứ mà một lần chạy liên tục 2
tuần không phủ được.

Probe được ghi ra **ngay khi xảy ra**, không tích lại cho `handleSummary()`. Một phiên bị
`lab-down` giết giữa chừng vẫn để lại toàn bộ probe đã lấy.

## 7. Chạy

```bash
make load-smoke                            # 12 request. LUÔN chạy trước.
make load-incluster SCENARIO=slo           # TIÊU CHÍ ĐỀ BÀI: 50 req/s, p95 < 3s
make load-ramp                             # tìm điểm gãy (~15 phút)
make load-steady RPS=2 DURATION=10m        # kết luận availability
make load-adversarial                      # 20% tấn công: safety có giữ khi quá tải?
make load-agents RPS=5 DURATION=10m        # 7 tenant, kiểm tra nhãn end_user
make load-soak DURATION=45m                # cache và trôi trong một phiên
make load-incluster SCENARIO=ramp          # bắt buộc cho tải > vài req/s
```

`load-smoke` tốn không đáng kể và là khác biệt giữa phát hiện một assertion hỏng **bây giờ**
với phát hiện nó sau 40 phút của một giờ GPU.

Tấn công lấy từ **fold B** của bộ 343 mẫu — tập held-out. Fold A là tập mà luật guardrail
được viết dựa trên nó, nên chặn được fold A không nói gì về khả năng tổng quát hoá; khi hỏi
"quá tải có làm giảm khả năng phát hiện không" thì chỉ tập held-out mới quy được nguyên nhân
cho tải chứ không cho việc học thuộc.

Câu hỏi lấy từ 144 truy vấn vàng trong `retrieval_eval.jsonl`, **không** từ
`bench/datasets/*.jsonl`. Prompt được pad tới đúng số token là thứ đúng cho `vllm bench
serve`, nhưng sai ở đây: prompt pad không retrieve được gì, không trích dẫn được gì, không
cache vào đâu — một load test dựng trên nó sẽ báo latency của một pipeline bị tắt mất phần
giữa.

## 8. Chi phí

Ramp ~15 phút GPU. Steady 10 phút. Cả hai cộng lại dưới 0,50 USD trên `g6.xlarge`, nên ràng
buộc thật không phải tiền mà là **thời gian phiên làm việc** — hạ tầng bị destroy mỗi tối,
nên load test phải nằm gọn trong phiên đã dựng cluster cho việc khác.

## 9. Chưa làm

- **Chưa chạy `load-slo` trên engine thật.** Đây là việc quan trọng nhất còn lại: tiêu chí
  50 req/s chưa có số đo nào, chỉ có phép tính. Cần 4 node GPU (vừa đúng quota) và một
  phiên có cluster.
- Chưa chạy lần nào trên engine thật. Toàn bộ logic phân loại đã được kiểm chứng end-to-end
  với một mock của pipeline (bao gồm các đường thất bại: tấn công lọt lưới, từ chối nhầm),
  nhưng con số latency đầu tiên phải đến từ GPU.
- `AGENT_KEYS` (virtual key thật cho từng agent, để kiểm tra budget và model allow-list)
  đã có đường dẫn vào nhưng chưa chạy cùng `make agent-keys`.
- Chưa đối chiếu p95 k6 đo được ở tầng caller với `litellm:e2e_seconds:p95` trong
  Prometheus. Hai con số này phải khớp; nếu lệch thì một trong hai đang đo sai thứ.
