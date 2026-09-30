# Profile serving 3 replica

Profile này mở rộng **LiteLLM và guardrail** lên 3 pod mỗi dịch vụ. Cấu hình lab mặc định
vẫn chạy 1 pod; chọn profile bằng `HA=1`. Service hiện có tự phân phối request tới các
pod sẵn sàng. Mỗi Deployment dùng rolling update `maxUnavailable: 0` và PodDisruptionBudget
`minAvailable: 2`.

## Điều kiện trước khi triển khai

1. Có PostgreSQL/Aurora dùng chung cho virtual key, quota và spend. Không dùng
   `ALLOW_NO_DB=1` ở profile này.
2. Có Redis **dùng chung, khả dụng qua nhiều AZ** với endpoint riêng trong VPC. Redis
   sidecar của lab là bộ nhớ riêng từng pod và không dùng được cho profile này. Secret
   `llm-serving/serving-redis` phải chứa `REDIS_URL` (dạng `rediss://...` nếu dùng TLS),
   `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`. LiteLLM đọc bốn biến này để chia sẻ
   rate limit và router state; guardrail dùng URL cho response cache. Cấp quyền kết nối
   Redis từ các node/pod serving và quản lý Secret ngoài Git.
3. Có ít nhất 2 node tooling ở 2 AZ, đủ CPU/RAM để đặt 6 pod serving cộng monitoring.
   Cấu hình đề xuất khi **tạo cluster mới** là `cpu_desired=3` và `node_subnet_count=2`
   trong `terraform/cluster/terraform.tfvars`. Với cluster đang chạy, EKS module bỏ qua
   thay đổi `desired_size` khi `terraform apply`; cập nhật node group qua EKS rồi xác
   nhận số node và AZ trước khi rollout. Profile yêu cầu trải pod qua ít nhất 2 hostname,
   nên rollout sẽ chờ nếu chỉ có một node hợp lệ.
4. Thực hiện migration database một lần trước khi rollout nhiều proxy. Nếu là database
   mới, `make litellm-up MODE=shared` với một replica sẽ thực hiện lần khởi tạo đầu;
   sau đó mới chọn `HA=1`. Khi nâng version LiteLLM, chạy migration riêng trước khi
   nâng 3 replica. Profile HA đặt `DISABLE_SCHEMA_UPDATE=true` trên các proxy, và chart
   hiện chưa có migration Job riêng.

## Triển khai

Tạo Secret từ nguồn quản lý bí mật của môi trường, rồi kiểm tra các key đã có (không in
giá trị). Nếu dùng file môi trường cục bộ, đặt file ngoài Git với quyền đọc hạn chế;
ví dụ `kubectl -n llm-serving create secret generic serving-redis
--from-env-file=/secure/path/serving-redis.env --dry-run=client -o yaml | kubectl apply -f -`.
Sau khi Redis, database và node đã sẵn sàng:

```bash
kubectl -n llm-serving get secret serving-redis
make guardrail-diff HA=1
make litellm-diff HA=1
make litellm-up HA=1 MODE=shared
kubectl -n llm-serving rollout status deploy/guardrail
kubectl -n llm-serving rollout status deploy/litellm
kubectl -n llm-serving get pods -o wide
make litellm-smoke
```

`make litellm-up HA=1` cài guardrail trước, rồi LiteLLM. Cache của guardrail sẽ tiếp
tục phục vụ theo đường thường nếu Redis tạm lỗi; khi đó cache hit giảm. Kiểm tra Redis,
quota chung giữa các pod, hai endpoint sẵn sàng sau khi chủ động xóa một pod, và các
dashboard lỗi/độ trễ trước khi kết luận HA hoạt động.

Ba replica **không** tự chứng minh uptime 99,5% trong pilot hai tuần. Ingress controller,
Redis, Aurora và vLLM cũng phải có khả năng chịu lỗi theo mục tiêu phục vụ. Báo cáo
`acceptance-report.md` đã có cận dưới availability 99,5946% ở 40 req/s trong phiên đo;
việc phủ 14 ngày pilot và p95 ở 50 req/s vẫn cần đo riêng.
