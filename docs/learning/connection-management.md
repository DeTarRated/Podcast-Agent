# 后端连接管理：短连接、长连接、Keep-Alive 与流式通信

- 定位：后端开发学习模块，可脱离具体项目阅读
- 范围：TCP、HTTP/1.x、HTTP/2、HTTP/3、RPC、WebSocket、SSE、连接池、超时、心跳、背压、可靠性与压测
- 核心目标：理解「连接是否复用」「请求何时结束」「连接由谁关闭」「失败是否可重试」这些不同维度的问题

## 先建立正确的概念模型

很多讨论把「长连接」说成一个模糊的性能开关，但实际系统里至少有这些不同对象：

| 对象 | 生命周期 | 例子 |
| --- | --- | --- |
| 进程 | 从启动到退出 | API 服务、数据库 worker |
| TCP 连接 | 建立、空闲、复用、关闭 | 客户端到服务的四次地址标识连接 |
| TLS 会话 | 建立在 TCP 之上 | 加密握手与会话恢复 |
| HTTP 请求 | 请求开始到响应完成 | 一次查询、一次审批 |
| RPC 调用 | 方法开始到结果或错误返回 | 一次内部领域调用 |
| 流 | 在一个连接或调用中持续传输消息 | 视频流、gRPC stream、SSE |
| 应用会话 | 用户登录到退出或过期 | Cookie session |
| 工作单元 | 入队到完成或失败 | 一个下载任务、一次转写 |

**长驻进程不等于长连接，长连接也不等于一次请求永远不结束。**

一个服务可以长期运行，同时每个请求都使用短连接；也可以长期运行，用同一个 TCP 连接承载大量彼此独立的 HTTP 请求。

## 三种常见通信形态

### 短连接

通常指一次请求或少量请求完成后关闭 TCP 连接：

```text
建立 TCP → TLS 握手 → 发送请求 → 接收响应 → 关闭连接
```

特点：

- 每次都需要连接建立成本
- 实现直观，连接状态少
- 大量短请求时可能增加延迟、CPU、端口与连接表压力
- 不适合高频请求复用，但并不意味着系统一定慢

### 持久连接／Keep-Alive 连接

连接在一个响应完成后不立即关闭，可以承载下一次请求：

```text
建立 TCP → TLS 握手
→ 请求 A → 响应 A
→ 请求 B → 响应 B
→ 请求 C → 响应 C
→ 空闲超时或主动关闭
```

这里的请求仍然是独立的。每个请求有自己的参数、结果、超时和错误。

### 长连接上的持续通信

连接不仅被复用，还承载持续的消息、事件或双向状态：

```text
建立连接
→ 消息 / 事件 / 心跳 / 状态更新持续发生
→ 显式关闭或异常断开
```

WebSocket、SSE 和 gRPC streaming 都属于这一类，但语义并不相同。

## TCP 建连与 TLS 的成本

一次网络请求通常不只是「发送一个 HTTP 包」：

```text
DNS 查询
→ TCP 三次握手
→ TLS 握手与证书验证
→ HTTP 请求与响应
→ 连接关闭或复用
```

TCP 三次握手：

```text
客户端                  服务端
   │ ---- SYN ---->          │
   │ <--- SYN-ACK ----       │
   │ ---- ACK ---->          │
```

连接复用可以减少：

- 重复 TCP 握手产生的往返延迟
- 重复 TLS 握手与证书计算
- 服务端 accept 和连接初始化成本
- 客户端与服务器的连接表、内存和文件描述符消耗

但不要简单理解成「长连接一定节省资源」。大量空闲连接长期占用内存和文件描述符，也会增加状态管理、超时和故障检测成本。

## HTTP/1.x 的短连接与持久连接

### HTTP/1.0 与 HTTP/1.1 的差别

HTTP/1.0 早期常见模型更接近每请求关闭连接。HTTP/1.1 默认使用持久连接，不需要每次发送 `Connection: keep-alive`。

常用头部：

```http
Connection: keep-alive
```

表示希望保持连接，但在 HTTP/1.1 中通常不是必需项。

```http
Connection: close
```

明确表示当前响应完成后关闭连接。

权威定义可阅读 [RFC 9112 · Persistence](https://www.rfc-editor.org/rfc/rfc9112.html#section-9.3) 与 [MDN · HTTP/1.x 连接管理](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Connection_management_in_HTTP_1.x)。

### 持久连接不等于永久连接

即使双方都没有发送 `Connection: close`，连接仍可能因为以下原因关闭：

- 客户端或服务端空闲超时
- 反向代理、负载均衡器或 NAT 回收状态
- 服务端连接最大生命周期
- 网络中断、进程重启或机器故障
- 连接池主动清理空闲连接
- 安全策略要求重新认证或重新协商

所以应用不能把池中的连接视为永远可用。

### Keep-Alive 与 HTTP 请求流水线

HTTP/1.1 可以复用连接，但普通请求通常是按顺序完成。HTTP pipelining 允许不等上一个响应就发送下一个请求，却要求响应按顺序返回，并容易受到队头阻塞和错误处理影响，因此没有成为通用的高并发解决方案。

**连接复用和请求并行不是同一件事。**

## HTTP/2：一个连接中的多个流

HTTP/2 将请求和响应拆成帧，并用 stream 标识不同请求：

```text
一个 TCP 连接
├── stream 1：请求 A
├── stream 3：请求 B
├── stream 5：请求 C
└── 控制帧：SETTINGS / PING / WINDOW_UPDATE 等
```

多个请求可以交错传输，不再要求整个响应严格按请求顺序完成。

它带来：

- 连接复用与多路复用
- 头部压缩
- 流级控制
- 服务端与客户端控制帧
- 一个连接承载多个并发请求

但 HTTP/2 仍运行在 TCP 之上。TCP 丢包或重传可能让同一连接里的多个流一起等待，这属于传输层队头阻塞。

HTTP/2 不使用 HTTP/1.1 的连接级头部来表达同样的语义。`Connection`、`Keep-Alive`、`Upgrade` 等连接专用字段不能直接照搬到 HTTP/2。

阅读：[Cloudflare · HTTP/2 For Web Developers](https://blog.cloudflare.com/http-2-for-web-developers/)。

## HTTP/3 与 QUIC

HTTP/3 使用 QUIC，而不是传统 TCP。QUIC 在 UDP 之上实现可靠传输、加密和多个独立流。

可以把它理解为：

- 传输与加密深度集成
- 减少建连与握手成本
- 多个流在协议层面独立
- 单个流丢包不必让所有其他流等待同一 TCP 重传序列

它仍然需要连接管理、超时、拥塞控制和背压。换成 HTTP/3 不会自动解决应用慢、队列积压或数据库过载。

## 请求响应、单向流与双向流

### 普通 RPC／HTTP 请求响应

```text
客户端：调用方法并等待
服务端：计算或访问依赖
服务端：返回一次结果或错误
```

适合查询、审批、一次性任务提交和内部领域操作。

### 服务端流

客户端发出一次请求，服务端持续返回多条消息：

```text
请求 → 消息 A → 消息 B → 消息 C → 结束
```

适合日志、进度、实时状态和事件订阅。

### 客户端流

客户端持续上传多条消息，服务端最后返回结果：

```text
消息 A → 消息 B → 消息 C → 完成
                         → 响应
```

适合分批上传和流式写入。

### 双向流

双方都可以持续发送消息：

```text
客户端 → A → C → E
服务端 → B → D → F
```

适合实时协作、交互式工具和状态同步。

## WebSocket

WebSocket 通过 HTTP 握手升级为独立的双向帧协议：

```http
GET /chat HTTP/1.1
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Key: ...
Sec-WebSocket-Version: 13
```

握手成功后，双方都可以发送消息，不需要每次新建 HTTP 请求。

适合：

- 在线聊天与实时协作
- 多人编辑与状态同步
- 游戏与实时交互
- 需要双向低频或高频消息的应用

需要注意：

- 浏览器 WebSocket 会自动处理部分 ping/pong，但应用仍需定义自己的存活与业务超时
- 不能只依赖 TCP keepalive 判断对端是否仍在工作
- 鉴权、Origin 校验、消息大小和发送背压不能省略
- 断线重连需要明确的消息幂等和状态恢复策略

阅读：[RFC 6455](https://www.rfc-editor.org/rfc/rfc6455.html) 与 [MDN · WebSocket](https://developer.mozilla.org/en-US/docs/Web/API/WebSocket)。

## Server-Sent Events

SSE 是一种基于 HTTP 的服务端单向事件流：

```http
Content-Type: text/event-stream
```

事件格式类似：

```text
id: event-42
data: {"status":"completed"}

```

浏览器可以使用 `EventSource` 接收，并支持自动重连语义。

适合：

- 通知与状态更新
- 任务进度
- 实时仪表盘
- 服务端向浏览器持续推送，但客户端不需要通过同一通道发送数据

SSE 与 WebSocket 的选择：

| 需求 | 更合适的选择 |
| --- | --- |
| 只要求服务端推送 | SSE |
| 需要双向消息 | WebSocket |
| 需要兼容普通 HTTP 基础设施 | SSE 或长轮询 |
| 需要自定义二进制消息 | WebSocket |
| 需要带类型的服务间流式 RPC | gRPC streaming |

阅读：[MDN · Using server-sent events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)。

## 长轮询

长轮询是客户端发送请求后，服务端暂时不返回，直到：

- 有新事件
- 到达等待超时
- 客户端取消或连接断开

客户端收到响应后立即发起下一次请求。

它不是持续流，而是多个长时间挂起的请求。优点是容易穿透传统 HTTP 代理，缺点是连接占用、超时处理和请求开销需要仔细管理。

## gRPC 与流式 RPC

gRPC 通常使用 HTTP/2，并通过 Protocol Buffers 定义类型。它支持：

- 单次 RPC
- 服务端流式 RPC
- 客户端流式 RPC
- 双向流式 RPC

示例：

```proto
service Tasks {
  rpc Watch (WatchRequest) returns (stream Event);
}
```

这表示一次调用产生一个持续事件流，而不是每次事件都创建一个新调用。

gRPC 的关键优势不是「连接永远不断」，而是：

- 明确的服务和消息契约
- 生成的客户端与服务端代码
- 多路复用和流式消息
- 截止时间、元数据、取消和状态码
- 跨语言服务通信

阅读：[gRPC · Core concepts](https://grpc.io/docs/what-is-grpc/core-concepts/)。

## 连接池是什么

连接池是客户端或中间件维护的一组可复用连接：

```text
请求到达
→ 从池获取空闲连接
→ 发送请求并接收响应
→ 连接归还池中
→ 下次继续复用
```

它通常负责：

- 限制总连接数或每主机连接数
- 维护空闲连接
- 清理失效连接
- 控制连接最大年龄
- 在连接耗尽时排队或拒绝
- 统计使用中、空闲、等待和关闭数量

### 连接池的关键参数

| 参数 | 含义 |
| --- | --- |
| 最大连接数 | 允许同时存在的连接上限 |
| 每主机最大连接数 | 防止单一上游被打满 |
| 最大空闲连接 | 控制空闲资源占用 |
| 空闲超时 | 空闲多久后关闭 |
| 最大生命周期 | 连接最多存在多久，便于负载均衡和刷新 |
| 获取连接超时 | 池耗尽后允许等待多久 |
| 等待队列上限 | 防止等待请求无限积累 |
| 失败清理策略 | 何时移除半死或异常连接 |

连接池不是越大越好。池过大可能只是把压力转移到数据库、上游服务或网络路径。

## 池耗尽与响应体释放

很多客户端只有在响应体被完整读取或关闭后，才能把连接归还池中。

危险模式：

```text
发送请求
→ 只读取响应头
→ 忘记读取或关闭响应体
→ 连接一直占用
→ 后续请求排队
→ 最终池耗尽
```

因此「连接池满」不一定是池太小，也可能是：

- 响应没有关闭
- 上游请求一直没有完成
- 读写超时缺失
- 下游重试放大
- 慢请求导致连接占用时间变长
- 空闲连接被对端关闭，但客户端尚未发现

可参考：[The Art of HTTP Connection Pooling](https://devblogs.microsoft.com/premier-developer/the-art-of-http-connection-pooling-how-to-optimize-your-connections-for-peak-performance/) 与 [连接池泄漏调试案例](https://blog.nigelsim.org/2011-06-17-debugging-connection-pool-leak-in-apache-http-client/)。

## 超时必须分层

不要只配置一个「请求超时」。一个调用通常需要多个不同超时：

| 超时 | 保护的对象 |
| --- | --- |
| DNS 超时 | 域名解析是否及时完成 |
| 连接超时 | TCP／TLS 是否能建立 |
| 写入超时 | 请求体是否能持续发送 |
| 读取超时 | 对端是否持续返回数据 |
| 空闲超时 | 无数据时连接何时关闭 |
| 连接池获取超时 | 多久拿不到连接就失败 |
| 单次 RPC 截止时间 | 一次方法调用的总预算 |
| 端到端业务截止时间 | 整个用户操作允许花多久 |
| 重试预算 | 所有重试合计允许花多久或多少次 |

读取超时不一定等于「整个响应允许的总时长」。有些实现把它定义为两次数据到达之间的最大间隔。必须阅读所用客户端文档。

推荐：[AWS Builders' Library · Timeouts, retries, and backoff with jitter](https://d1.awsstatic.com/builderslibrary/pdfs/timeouts-retries-and-backoff-with-jitter.pdf)。

## 空闲超时与 keepalive 竞争

客户端、服务端、代理和 NAT 可能各自有不同的空闲超时：

```text
客户端认为连接仍可复用
→ 中间设备已回收状态
→ 客户端复用旧连接
→ 连接被重置或请求失败
```

这就是典型的 stale connection／keepalive race。解决思路不是把客户端超时无限调大，而是：

- 了解路径上每一跳的空闲策略
- 让客户端空闲时间小于中间设备的回收时间
- 设置合理的连接最大生命周期
- 使用连接健康检查
- 对安全、可重试请求处理失效连接
- 不把连接失败与业务失败混为一谈

AWS 对不同负载均衡器有不同空闲默认值，例如 NLB TCP flow 默认空闲约 350 秒、ALB 默认连接空闲约 60 秒，具体应以当前文档和实际配置为准。参考：[AWS NLB 文档](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/network-load-balancers.html)。

## TCP keepalive、协议 ping/pong 与应用心跳

### TCP keepalive

TCP keepalive 是操作系统层的空闲探测，主要用来发现长期无数据的对端异常。RFC 1122 将它设计为可选能力，并规定启用后的默认探测间隔不得小于约两小时；这通常远慢于应用需要的故障检测。

它不能替代：

- HTTP 请求超时
- RPC 截止时间
- 数据库事务超时
- worker 任务租约
- 应用业务心跳

阅读：[RFC 1122 · Communication Layers](https://datatracker.ietf.org/doc/html/rfc1122)。

### 协议 ping/pong

HTTP/2 有 PING 控制帧，WebSocket 有 ping/pong 帧。它们可以验证协议对端是否仍能响应，但具体是否自动处理取决于客户端、服务端和代理实现。

### 应用心跳

应用心跳表达的是业务存活：

```text
worker 仍持有任务
服务仍愿意接受该连接
会话仍然有效
对端仍具备处理消息的能力
```

例如任务调度中的心跳用于延长任务租约；它与 TCP keepalive 没有直接关系。

### TCP_USER_TIMEOUT

部分系统还提供 `TCP_USER_TIMEOUT`，用于限制已发送但未获确认的数据持续重传的时间。它与空闲 keepalive 探测不是同一参数。可参考 [Code Arcana · TCP Keepalive is a lie](https://codearcana.com/posts/2015/08/28/tcp-keepalive-is-a-lie.html)，并结合操作系统当前文档理解。

## 重试、幂等与结果未知

网络错误发生时，请求可能处于不同阶段：

```text
尚未发出
已发出，服务端未处理
服务端已处理，客户端未收到响应
客户端已收到响应，但本地结果未落盘
```

只有部分情况适合自动重试。

- 查询类请求通常可安全重试，但仍需超时和次数限制
- 创建订单、付费调用、审批等写操作需要幂等键或服务端去重
- 如果服务端已经处理而客户端不知道结果，直接重试可能重复执行
- 重试需要指数退避、随机抖动和总预算，避免所有客户端同时再次冲击服务
- 对非幂等操作，应先查询结果或对账，而不是立即重发

阅读：[AWS · Making retries safe with idempotent APIs](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/)。

## 背压与流量控制

背压是下游处理不过来时，让上游减慢、等待或拒绝，而不是无限接收。

### TCP 流量控制

TCP 使用接收窗口告诉发送方当前还能接收多少数据。窗口变小，发送方就需要等待。这是传输层背压。

### HTTP/2 流量控制

HTTP/2 同时有连接级和流级窗口。某个流消费慢，可以只限制那个流，而不必停止整个连接。

### 应用层背压

应用仍需自己的限制：

- 有界队列
- 最大并发任务数
- 最大请求体
- 最大消息大小
- 获取连接超时
- 写缓冲区上限
- 消费速度低于生产速度时的暂停或拒绝

协议流量控制不能替代业务队列和预算控制。

可参考：[Backpressure in Client-Server Applications](https://travishaagen.github.io/posts/backpressure-in-client-server-applications/)。

## 连接数、QPS 与延迟的关系

不要只问「系统支持多少 QPS」。至少同时观察：

- 每秒请求数
- 请求平均与 p95／p99 延迟
- 同时在途请求数
- 连接总数与每连接请求数
- 队列长度和等待时间
- 错误率与超时率
- CPU、内存、文件描述符和网络带宽

可以用一个简化关系帮助估算：

```text
平均并发请求数 ≈ QPS × 平均请求耗时（秒）
```

例如：

- 50 QPS、平均 20ms，平均在途约 1 个请求
- 50 QPS、平均 2s，平均在途约 100 个请求

同一 QPS 下，慢请求会显著增加连接和内存占用。因此容量问题常常首先是延迟与连接占用问题，而不是请求数量本身。

## 文件描述符、端口与 TIME_WAIT

高并发连接会消耗：

- 每个 socket 的文件描述符
- 读／写缓冲区内存
- 服务端连接表和中间设备状态
- 客户端本地端口与 NAT 映射

大量短连接还可能增加 TIME_WAIT 数量，但 TIME_WAIT 本身是 TCP 正常机制，不应看到一个数量就盲目调整内核参数。

优先顺序通常是：

- 减少不必要的建连
- 复用连接
- 限制连接池和等待队列
- 缩短异常请求占用时间
- 检查响应体是否释放
- 再评估操作系统和网络设备容量

## 长连接的安全问题

长连接并不天然更安全。需要额外考虑：

- TLS 证书、加密与身份认证
- WebSocket 的 Origin 和握手鉴权
- 不把长期 token 放在容易被记录的 URL query 中
- 单连接消息速率与消息大小限制
- 慢速发送、慢速读取和慢速攻击
- 连接空闲但持续占用资源的 DoS 风险
- 断线后权限是否仍然有效
- 用户被禁用后是否应主动断开现有连接

长连接容易让「建立时鉴权」变成「永远有效」。需要明确会话过期、权限撤销和重认证策略。

## 可观测性字段

连接与请求监控可以记录：

- connection id 或内部请求 id
- 客户端、上游和协议版本
- 建连耗时、TLS 耗时、首字节时间、总耗时
- 当前连接数、空闲连接数、等待连接数
- 请求是否复用连接
- 读取和写入字节数
- 超时类型、取消原因和重试次数
- 流的消息数、持续时间和背压等待时间

不要记录：

- 密码和 API key
- 完整 Cookie 或 Authorization
- 含签名 query 的完整私有 URL
- 用户隐私内容或完整业务文本

## 一个典型故障如何排查

假设「偶发请求失败，连接池逐渐耗尽」：

- 先看请求延迟和池等待时间，而不是立即加大池
- 检查响应体是否完整消费并关闭
- 检查上游是否存在长时间不返回的请求
- 检查连接、读取、写入和业务截止时间是否匹配
- 检查中间设备的空闲超时是否小于客户端复用时间
- 检查重试是否放大流量
- 检查是否存在未取消的流或长轮询
- 检查错误发生在发送前、处理中还是响应回传阶段

「增加连接数」只能改变容量，不能修复连接泄漏或结果未知。

## 建议动手实验

### 用 curl 观察 HTTP 连接

目标：

- 使用 `curl -v` 查看建连、TLS 和响应过程
- 连续请求同一服务，观察是否复用连接
- 分别使用 `Connection: close` 与默认行为比较
- 用 tcpdump 或 Wireshark 观察 SYN、FIN、RST 和重传

需要记录：

- 每次是否重新握手
- 首字节时间和总时间
- 响应结束后连接是否关闭
- 哪一端主动关闭

### 写一个连接池实验

目标：

- 创建一个小型连接池
- 让上游故意延迟响应
- 观察池耗尽、等待队列和获取超时
- 对比完整读取响应与不释放响应的结果
- 给等待队列设置上限，观察背压

需要记录：

- 使用中连接数
- 池等待时间
- 请求成功率和延迟分布
- 恢复后连接是否重新可用

### 写一个 SSE 服务

目标：

- 服务端每秒发送一个事件
- 浏览器使用 EventSource 接收
- 主动断开服务并观察自动重连
- 使用事件 id 或最后事件标识恢复状态
- 配置代理和空闲超时，观察长响应何时被中断

### 写一个 WebSocket 服务

目标：

- 客户端与服务端双向发送消息
- 实现 ping/pong 和应用心跳
- 模拟对端不响应与网络断开
- 实现消息幂等键，避免重连后重复执行
- 限制发送缓冲区和消息大小

### 写一个 gRPC streaming 示例

目标：

- 定义服务端流和双向流
- 观察一个 RPC 调用内的多消息传输
- 使用 deadline 和取消
- 测试流中途失败的恢复语义

### 做一次有边界的压测

使用 k6、autocannon 或自写客户端，分别测量：

- 短连接请求
- Keep-Alive 复用请求
- HTTP/2 多路复用请求
- 长连接空闲和持续消息
- 上游变慢时的队列与连接占用

报告至少包含：

- QPS、并发数、p50／p95／p99 延迟
- 错误率、超时率和连接数
- CPU、内存、文件描述符和带宽
- 测试环境、客户端数量和是否使用 mock 上游

## 面试与自测问题

### 「长连接一定比短连接好吗？」

不一定。连接复用降低建连成本，但长期空闲连接会消耗内存、文件描述符和中间设备状态。需要结合请求频率、延迟、空闲时间、并发量和故障恢复成本判断。

### 「HTTP Keep-Alive 就是长连接吗？」

它通常表示持久连接和连接复用。一次 HTTP 请求仍然可以很快完成。它与持续双向通信的长连接不是完全相同的概念。

### 「长驻服务和长连接有什么区别？」

长驻服务是进程一直运行。长连接是网络连接持续存在。服务可以长期运行但每个请求都建立新连接，也可以复用连接处理大量请求。

### 「HTTP/2 是否已经解决所有队头阻塞？」

没有。它解决的是应用请求／响应层面的多路复用问题，但底层 TCP 丢包仍可能让多个流一起等待。HTTP/3 的 QUIC 进一步改进了传输层独立流的丢包影响。

### 「连接池是不是越大越好？」

不是。池大小应匹配上游容量和调用延迟。过大的池可能压垮数据库或供应商；过小的池会排队。还需要获取超时、等待队列上限和背压。

### 「为什么响应已经返回，连接还不能复用？」

常见原因是响应体未完整读取或关闭、连接仍处于流式状态、协议发生错误、对端已关闭，或客户端正在等待连接清理。

### 「TCP keepalive 能否替代 RPC 超时？」

不能。TCP keepalive主要处理连接空闲探测，RPC 超时保护的是单次调用的业务时间预算。两者需要同时设计。

### 「服务端返回 200，客户端为什么还可能失败？」

服务端生成并发送成功响应，不代表客户端完整收到并解析。响应可能在代理、网络或客户端读取阶段中断，也可能响应体格式不符合预期。

### 「看到 RemoteProtocolError 就可以直接重试吗？」

不能直接下结论。需要判断请求是否幂等、服务端是否已执行、是否存在请求标识或结果查询方式。对付费或写入操作，盲目重试可能重复执行。

### 「长连接如何避免用户离线后仍占用资源？」

使用空闲超时、应用心跳、最大连接生命周期、消息速率限制、连接数配额和权限撤销策略，不能只依赖 TCP 层探活。

### 「QPS 高就一定要使用长连接吗？」

不一定。还要看请求耗时和连接复用收益。高 QPS 短请求可以通过连接池、HTTP/2、多实例和缓存扩展；长连接只是其中一种工具。

## 推荐学习顺序

### 基础模型

- 能区分进程、TCP 连接、HTTP 请求、RPC 调用、流和会话
- 能画出短连接、Keep-Alive 和持续流的生命周期
- 理解握手、TLS、请求、响应和关闭分别发生在哪里

### 协议能力

- 阅读 HTTP/1.1 持久连接规则
- 理解 HTTP/2 stream 与 HTTP/3 QUIC 的差异
- 分别实现 SSE、WebSocket 和 gRPC streaming 的小示例
- 了解长轮询及其与流式通信的差异

### 可靠性能力

- 为调用配置连接、读取、写入、池获取和端到端超时
- 理解 TCP keepalive、ping/pong 和应用心跳
- 能分析 half-open、stale connection 和 keepalive race
- 能把重试、幂等、退避和结果查询放在同一个故障模型中

### 容量与工程能力

- 会计算请求延迟与并发连接的关系
- 能解释连接池耗尽、响应体泄漏和背压
- 会使用压测数据而不是凭感觉谈高并发
- 能同时考虑安全、权限撤销、费用和资源上限

## 外部阅读与学习资源

### 协议与官方文档

- [RFC 9112 · HTTP/1.1 持久连接](https://www.rfc-editor.org/rfc/rfc9112.html#section-9.3)：HTTP/1.1 持久连接与关闭的权威规则
- [MDN · HTTP/1.x 连接管理](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Connection_management_in_HTTP_1.x)：短连接、持久连接和 pipelining 的实践说明
- [RFC 6455 · WebSocket Protocol](https://www.rfc-editor.org/rfc/rfc6455.html)：握手、帧、ping/pong 与关闭
- [MDN · Server-Sent Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)：SSE 格式、EventSource 与重连
- [gRPC · Core concepts](https://grpc.io/docs/what-is-grpc/core-concepts/)：单次与流式 RPC、截止时间和生命周期
- [Cloudflare · HTTP/2 For Web Developers](https://blog.cloudflare.com/http-2-for-web-developers/)：HTTP/2 多路复用与连接复用
- [Cloudflare · Connection coalescing](https://blog.cloudflare.com/connection-coalescing-experiments/)：进阶连接合并与复用
- [Cloudflare · Connection reuse](https://developers.cloudflare.com/smart-shield/concepts/connection-reuse/)：代理／回源场景的连接复用

### 可靠性与工程实践

- [AWS Builders' Library · Timeouts, retries, and backoff with jitter](https://d1.awsstatic.com/builderslibrary/pdfs/timeouts-retries-and-backoff-with-jitter.pdf)：超时、重试和退避的系统化设计
- [AWS Builders' Library · Making retries safe with idempotent APIs](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/)：写入与重试的幂等保护
- [AWS · Network Load Balancers](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/network-load-balancers.html)：长连接、空闲超时和负载均衡器行为
- [AWS · RDS Proxy 连接借用超时](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/rds-proxy-best-practices.configuration.html)：连接池获取与容量控制
- [Code Arcana · TCP Keepalive is a lie](https://codearcana.com/posts/2015/08/28/tcp-keepalive-is-a-lie.html)：TCP 探测、故障检测和 TCP_USER_TIMEOUT 的工程讨论
- [Travis Haagen · Backpressure in Client-Server Applications](https://travishaagen.github.io/posts/backpressure-in-client-server-applications/)：客户端与服务端背压
- [Microsoft · The Art of HTTP Connection Pooling](https://devblogs.microsoft.com/premier-developer/the-art-of-http-connection-pooling-how-to-optimize-your-connections-for-peak-performance/)：连接池复用与池耗尽
- [Nigel Sim · Debugging a pool leak](https://blog.nigelsim.org/2011-06-17-debugging-connection-pool-leak-in-apache-http-client/)：响应未释放导致连接无法归还的案例
- [Stackademic · HTTP 连接池耗尽案例](https://blog.stackademic.com/python-httpx-connection-pool-exhaustion-took-down-our-webhook-worker-for-47-minutes-3ec59949ad47)：作为二手事故材料阅读，重点核对其中的连接状态分析，不把单一文章当成协议规范

## 学完后的自检验收

- 能给出一个请求从 DNS、TCP、TLS 到响应结束的完整时序
- 能解释 Keep-Alive、连接池、WebSocket、SSE 和 gRPC streaming 的不同用途
- 能为一个调用设计连接、读取、写入、空闲、池获取和总截止时间
- 能区分 TCP keepalive、协议 ping/pong 和应用任务心跳
- 能分析为什么「服务端 200」仍可能导致客户端结果未知
- 能说明连接复用、队列背压、重试幂等和预算控制之间的关系
- 能通过实验与监控数据证明容量，而不是只凭「长连接更快」做结论
