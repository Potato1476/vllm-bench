# Khoá công khai của các bên được phép ký model

Mỗi file `<tên>.pub` là khoá công khai của một bên được ghi trọng số lên S3. Tên file
chính là tên dùng trong `allowed_signers` của `models/catalog.yaml`.

Khoá công khai **không phải bí mật**. Để chúng trong git là có chủ ý: thêm hay gỡ một bên
được phép ký là thay đổi ai có thể đưa model vào production, và việc đó phải đi qua PR
như mọi thay đổi khác, chứ không phải là một lệnh ai đó chạy trên máy mình.

Khoá bí mật nằm trong AWS KMS và không bao giờ rời khỏi đó. Quyền ký là quyền IAM
`kms:Sign` trên đúng một khoá; CloudTrail ghi lại mọi lần ký.

Thêm một bên ký:

    aws kms create-key --key-spec ECC_NIST_P256 --key-usage SIGN_VERIFY
    aws kms create-alias --alias-name alias/model-signer-<tên> --target-key-id <id>
    cosign public-key --key awskms:///alias/model-signer-<tên> > deploy/signers/<tên>.pub

rồi mở PR thêm `<tên>` vào `allowed_signers`.
