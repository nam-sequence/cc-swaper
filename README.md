# cc-swaper (`ccs`)

CLI để dùng nhiều tài khoản **Claude Code / claude.ai đã đăng nhập hợp lệ**. Bạn có thể chọn tài khoản thủ công hoặc để `ccs` nhận biết thông báo hết usage và tiếp tục cùng conversation trên tài khoản kế tiếp.

## Cài đặt

Yêu cầu `uv`, Python 3.10+, Claude Code và `tmux` trên `PATH`. Chế độ dịch vụ thường trực dùng macOS LaunchAgent. Đã thử trực tiếp với Claude Code 2.1.280. Installer không tự tải hoặc chạy script từ Internet; hãy cài `uv` trước theo một trong các cách sau:

```bash
brew install uv                  # macOS với Homebrew
python3 -m pip install --user uv # máy có pip và cho phép cài user package
```

Nếu không dùng Homebrew hoặc pip, xem [hướng dẫn cài uv chính thức](https://docs.astral.sh/uv/getting-started/installation/), sau đó kiểm tra:

```bash
uv --version
```

```bash
./scripts/install.sh              # cài CLI, tích hợp Zsh và khởi động dịch vụ nền
ccs list                         # account Claude Code hiện tại được đăng ký là "main"
ccs add secondary                # mở trang đăng nhập Claude chính thức cho account thứ hai
ccs list --show-identity         # kiểm tra email/org, tránh đăng nhập trùng account
```

Nếu đã cài bằng `uv tool install .`, chạy `ccs init` một lần (nếu chưa có profile) rồi `ccs setup` để bật Zsh và LaunchAgent. Nếu chưa muốn mở trình duyệt: `ccs add secondary --no-login`, sau đó `ccs login secondary`. `ccs` không yêu cầu mật khẩu hay token. Mỗi profile mới có thư mục cấu hình Claude riêng; account mặc định dùng cấu hình hiện có của máy.

## Dùng hằng ngày

```bash
cd /path/to/project
claude                           # bắt đầu phiên nền; terminal trả về ngay
ccs attach                       # vào phiên của project hiện tại
# Ctrl-b d                         rời tmux; phiên Claude tiếp tục chạy
ccs status                       # xem dịch vụ và các phiên đang được giám sát
ccs usage                        # xem usage 5 giờ/7 ngày của tất cả account
ccs usage main                   # chỉ xem một profile
ccs usage --json                 # dữ liệu có cấu trúc cho script
ccs stop                         # dừng phiên nền của project hiện tại
ccs run --foreground             # chạy trực tiếp trong terminal như trước
ccs use secondary                # sau khi thoát Claude, chọn profile cho lần chạy sau
ccs resume                       # tiếp tục conversation cuối trong một phiên nền mới
ccs switch main                  # đổi thủ công và tiếp tục conversation cuối
ccs run --no-auto -- -p "Hi"     # chế độ không tương tác, không tự chuyển
```

`ccs run -- --model sonnet` chuyển tham số sang lần khởi chạy Claude đầu tiên. Khi tự chuyển account, `ccs` tạo nhánh phiên mới từ transcript bằng `claude --resume <đường-dẫn-transcript> --fork-session`. Transcript cũ giữ nguyên. Nó đưa một lời nhắc thận trọng để Claude kiểm tra trạng thái trước khi tiếp tục, và dùng permission mode `manual` cho lượt tiếp tục đầu tiên. Không phát lại nguyên prompt trước đó, vì prompt ấy có thể đã sửa file hoặc gọi công cụ trước khi hết usage. Cờ Claude truyền cho lần khởi chạy đầu tiên không được truyền lại ở lần chuyển tự động; hãy cấu hình các lựa chọn cần duy trì trong từng profile.

Sau cài đặt, mở terminal Zsh mới và gõ `claude`: `ccs` tạo một tmux session theo thư mục project, trả terminal về ngay và tiếp tục giám sát quota trong session đó. Dịch vụ LaunchAgent luôn chạy để ghi nhận trạng thái các phiên `ccs` quản lý; đóng terminal không dừng chúng. `claude -c` chuyển sang `ccs resume`. Lệnh quản trị như `claude auth status`, `claude --version` và chế độ `-p`/`--bg` đi qua `ccs native` trên profile đã chọn; các chế độ native này không có tự chuyển account. Phiên Claude mở trực tiếp ngoài wrapper không được dịch vụ tự đổi account.

Mỗi project có một phiên nền. Nếu phiên đang tồn tại, `claude` không mở thêm phiên thứ hai; dùng `ccs attach` để quay lại hoặc `ccs stop` để dừng trước khi khởi chạy lại.

`ccs usage` gọi lệnh `/usage` cục bộ của Claude Code riêng cho từng profile, không tạo transcript hoặc lượt model. Trong terminal, lệnh hiện trạng thái tải cho từng account, rồi hiển thị thanh phần trăm và mốc reset 5 giờ/7 ngày đúng như Claude trả về, kể cả giới hạn 7 ngày theo model nếu có. Khi Claude không cung cấp mốc reset (ví dụ phiên 5 giờ đang ở 0%), CLI ghi rõ là chưa có dữ liệu thay vì tự tính. `ccs usage --json` giữ output có cấu trúc và không hiện animation. [Tài liệu `/usage`](https://code.claude.com/docs/en/commands).

Để xoá account phụ khỏi CLI:

```bash
ccs remove secondary              # logout, bỏ profile khỏi danh sách, lưu lịch sử trong ~/.config/cc-swaper/removed
ccs remove secondary --purge-data # logout và xoá luôn dữ liệu Claude cục bộ của profile này
```

Thoát các phiên đang dùng profile trước khi xoá. `main` (account Claude mặc định của máy) không thể xoá bằng `ccs remove`. Lệnh `--purge-data` không đụng đến `~/.claude`. Để gỡ toàn bộ CLI, dùng `ccs service uninstall`, `ccs shell uninstall`, rồi `uv tool uninstall cc-swaper`.

`ccs` chỉ chuyển khi hook `StopFailure` của Claude Code báo `rate_limit` **và** kèm thông báo giới hạn tài khoản rõ ràng, chẳng hạn `You've hit your limit · resets ...` hoặc `This request would exceed your account's rate limit`. Dòng chữ tương tự trong câu trả lời bình thường không gây chuyển. Lỗi `429` chung, lỗi mạng và lỗi xác thực không gây chuyển account. Nếu hooks bị vô hiệu hóa hoặc Anthropic đổi định dạng lỗi, tự chuyển có thể không kích hoạt; bạn vẫn có thể thoát phiên rồi dùng `ccs switch <profile>`. CLI từ chối `--bare`, `--safe-mode` trong auto mode và từ chối chế độ không lưu transcript. Nếu hết tài khoản đã đăng nhập, CLI dừng và giữ transcript để bạn dùng `ccs resume` sau. `ccs list` chỉ kiểm tra trạng thái đăng nhập, không đọc quota từ server.

## Giới hạn của việc tiếp tục phiên

Claude Code **phải khởi chạy lại process** khi đổi account OAuth. CLI giữ nguyên thư mục làm việc và tạo nhánh từ toàn bộ lịch sử conversation; nó không thể tiếp tục chính xác một token đang sinh hoặc một lệnh bên ngoài đang chạy. CLI chờ transcript ngừng ghi trong một khoảng ngắn trước khi dừng process cũ, nhưng không thể bảo đảm một thao tác đang dở đã hoàn tất. Lời nhắc tiếp tục yêu cầu Claude xem lại trạng thái để tránh lặp thao tác. Chuyển transcript giữa hai `CLAUDE_CONFIG_DIR` bằng đường dẫn tuyệt đối được xây trên khả năng `--resume <transcript path>` và `--fork-session` của Claude Code. Trên máy này với Claude Code 2.1.280, một thử nghiệm nhỏ giữa hai account khác nhau đã xác nhận nhánh mới giữ ngữ cảnh và transcript nguồn không đổi. Việc chuyển sau **usage limit thật** vẫn chưa được kiểm chứng; Anthropic cũng chưa cam kết riêng luồng resume giữa hai account. Nếu Claude từ chối mở transcript ở profile mới, file phiên cũ vẫn ở nguyên vị trí.

`ccs` không đọc, sao chép hoặc lưu credential. Trên macOS, Claude Code quản lý credential trong Keychain; `ccs` chỉ gọi `claude auth login` và `claude auth status`. Registry của `ccs` nằm tại `~/.config/cc-swaper` (có thể đổi bằng `CC_SWAPER_HOME`) và chỉ chứa tên profile, đường dẫn cấu hình, lựa chọn hiện tại, ID và đường dẫn transcript. Email/org chỉ được đọc từ `auth status` để hiển thị theo yêu cầu và tránh tự chuyển vào cùng account. Biến môi trường `ANTHROPIC_*` có thể ghi đè đăng nhập claude.ai được loại khỏi process con. Proxy/CA và cấu hình Claude nằm trong project vẫn được dùng; hãy chạy CLI trong shell và project bạn tin cậy. Khi chuyển account, toàn bộ lịch sử chat và tool output trong transcript cũ được đưa vào ngữ cảnh của account mới.

Tài liệu Claude Code: [nhiều account với `CLAUDE_CONFIG_DIR`](https://code.claude.com/docs/en/env-vars), [nơi lưu credential](https://code.claude.com/docs/en/authentication), [resume và fork](https://code.claude.com/docs/en/sessions), [hook `StopFailure`](https://code.claude.com/docs/en/hooks).
