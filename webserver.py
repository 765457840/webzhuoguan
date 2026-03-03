from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_sock import Sock  # WebSocket依赖
import time
import os
import sys
import uuid
import hashlib
import threading
import json  # 用于前端WebSocket消息序列化

# ==================== 授权校验配置（保持不变） ====================
SECRET_KEY = "your_custom_secret_key_2026"
CODE_LENGTH = 8

valid_expire_list = [
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10,
    11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
    21, 22, 23, 24, 25, 26, 27, 28, 29, 30,
    31, 32, 33, 34, 35, 36, 37, 38, 39, 40,
    41, 42, 43, 44, 45, 46, 47, 48, 49, 50,
    51, 52, 53, 54, 55, 56, 57, 58, 59, 60,
    120, 180, 240, 300, 360, 420, 480, 540, 600, 660,
    720, 780, 840, 900, 960, 1020, 1080, 1140, 1200, 1260,
    1320, 1380, 1440,
    2880, 4320, 5760, 7200, 8640, 10080,
    11520, 12960, 14400, 15840, 17280, 18720, 20160, 21600,
    23040, 24480, 25920, 27360, 28800, 30240, 31680, 33120,
    34560, 36000, 37440, 38880, 40320, 41760, 43200
]

# -------------------------- 全局存储（含WebSocket相关） --------------------------
auth_code_usage = {}
task_queue = {}
pending_tasks = []  # 待处理任务队列（供WebSocket推送）

# 本地脚本WebSocket连接池
websocket_connections = []
conn_lock = threading.Lock()

# 前端页面WebSocket连接池 + 待推送消息队列
frontend_ws_connections = []
frontend_conn_lock = threading.Lock()
pending_frontend_messages = []  # 待推送的前端消息队列


# ==================== 路径兼容处理（保持不变） ====================
def get_resource_path(relative_path):
    if hasattr(sys, '_MEIPASS'):
        base_path = os.path.dirname(sys.executable)
    else:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


# ==================== 初始化Flask + WebSocket =====================
app = Flask(__name__, static_folder=get_resource_path('frontend'), static_url_path='')
CORS(app, supports_credentials=True)  # 增强跨域配置
sock = Sock(app)  # 初始化WebSocket


# ==================== 授权码校验函数（保持不变） ====================
def verify_auth_code(input_code: str, only_check: bool = False) -> tuple[bool, str]:
    if not input_code or input_code.count('_') != 3:
        return False, "授权码格式错误！"
    salt, generate_ts, use_limit, input_hash = input_code.split('_', 3)

    if not generate_ts.isdigit() or not use_limit.isdigit():
        return False, "授权码格式错误！"
    generate_ts = int(generate_ts)
    use_limit = int(use_limit)
    current_ts = int(time.time())

    if input_code in auth_code_usage:
        remaining = auth_code_usage[input_code]
        if remaining <= 0:
            return False, "授权码使用次数已耗尽！"
    else:
        auth_code_usage[input_code] = use_limit
        remaining = use_limit

    for expire_m in valid_expire_list:
        raw_str = f"{salt}_{expire_m}_{use_limit}_{generate_ts}_{SECRET_KEY}"
        verify_hash = hashlib.md5(raw_str.encode('utf-8')).hexdigest()[:16]
        if verify_hash == input_hash and (current_ts - generate_ts) <= expire_m * 60:
            if not only_check:
                auth_code_usage[input_code] -= 1
                remaining = auth_code_usage[input_code]
            return True, f"授权码有效，剩余使用次数：{remaining} 次"
    return False, "授权码无效/已过期/密钥不匹配！"


# ==================== 原有HTTP接口（保留前端相关） ====================
@app.route('/')
def index():
    return send_from_directory(get_resource_path('frontend'), 'index.html')


@app.route('/submit_number', methods=['POST'])
def submit_number():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': '请求数据为空！'})

        # 授权码校验
        auth_code = data.get('auth_code', '').strip()
        check_result, check_msg = verify_auth_code(auth_code, only_check=False)
        if not check_result:
            return jsonify({'success': False, 'message': check_msg})

        # 验证动态码
        input_number = data.get('number', '').strip()
        if len(input_number) != 8 or not input_number.isdigit():
            return jsonify({'success': False, 'message': '请输入有效动态码！'})

        # 创建任务
        task_id = str(uuid.uuid4())
        task_queue[task_id] = {
            "number": input_number,
            "status": "pending",
            "result": "",
            "create_time": time.time()
        }
        pending_tasks.append({"task_id": task_id, "number": input_number})  # 加入待推送队列

        # 实时推送任务到所有连接的本地脚本WebSocket
        with conn_lock:
            for conn in websocket_connections:
                try:
                    conn.send(f"task:{task_id}:{input_number}")  # 推送格式：task:任务ID:数字
                except Exception as e:
                    print(f"推送任务失败：{e}")
                    websocket_connections.remove(conn)

        return jsonify({
            'success': True,
            'message': f'动态码已提交，等待处理...\n{check_msg}',
            'task_id': task_id
        })
    except Exception as e:
        return jsonify({'success': False, 'message': f'提交失败：{str(e)}'})


@app.route('/get_result/<task_id_param>', methods=['GET'])
def get_result(task_id_param):
    if task_id_param not in task_queue:
        return jsonify({'success': False, 'message': '无效任务ID！'})

    task_info = task_queue[task_id_param]
    return jsonify({
        'success': True,
        'is_processing': task_info["status"] == "processing" or task_info["status"] == "pending",
        'result': task_info["result"] if task_info["result"] else "处理中..."
    })


# ==================== 推送结果到前端WebSocket（带重试） ====================
def push_result_to_frontend(task_id, number, result):
    """向所有前端WebSocket推送任务结果（失败则加入重试队列）"""
    # 构造消息
    msg = json.dumps({
        "type": "task_result",
        "data": {
            "task_id": task_id,
            "number": number,
            "result": result
        }
    })

    # 先加入待推送队列
    pending_frontend_messages.append(msg)

    with frontend_conn_lock:
        # 遍历所有前端连接推送
        for conn in frontend_ws_connections:
            try:
                conn.send(msg)
                # 推送成功则从队列移除
                if msg in pending_frontend_messages:
                    pending_frontend_messages.remove(msg)
                print(f"✅ 已向前端推送任务{task_id}结果")
            except Exception as e:
                print(f"向前端推送结果失败，保留至重试队列：{e}")
                frontend_ws_connections.remove(conn)

        # 尝试重推队列中未发送的消息
        if pending_frontend_messages:
            print(f"📥 尝试重推{len(pending_frontend_messages)}条未发送的前端消息")
            for pending_msg in pending_frontend_messages[:]:
                for conn in frontend_ws_connections:
                    try:
                        conn.send(pending_msg)
                        pending_frontend_messages.remove(pending_msg)
                    except:
                        continue


# ==================== WebSocket核心接口（本地脚本） =====================
@sock.route('/ws/tasks')
def handle_websocket(ws):
    """处理本地脚本的WebSocket连接：推送任务+接收结果"""
    print("✅ 本地脚本WebSocket连接成功")
    with conn_lock:
        websocket_connections.append(ws)  # 将连接加入池

    try:
        # 推送历史待处理任务
        for task in pending_tasks:
            ws.send(f"task:{task['task_id']}:{task['number']}")

        # 循环接收客户端消息（结果回传）
        while True:
            message = ws.receive()
            if not message:
                break

            # 解析结果：格式为 result:任务ID:处理结果
            if message.startswith('result:'):
                parts = message.split(':', 2)
                if len(parts) == 3:
                    task_id = parts[1]
                    result = parts[2]
                    if task_id in task_queue:
                        task_queue[task_id]["result"] = result
                        task_queue[task_id]["status"] = "finished"
                        # 推送结果到前端
                        push_result_to_frontend(task_id, task_queue[task_id]["number"], result)
                        # 移除已完成任务
                        if {"task_id": task_id, "number": task_queue[task_id]["number"]} in pending_tasks:
                            pending_tasks.remove({"task_id": task_id, "number": task_queue[task_id]["number"]})
                        print(f"✅ 收到任务{task_id}结果：{result}")
    except Exception as e:
        print(f"❌ 本地脚本WebSocket异常：{e}")
    finally:
        with conn_lock:
            if ws in websocket_connections:
                websocket_connections.remove(ws)
        print("❌ 本地脚本WebSocket连接断开")


# ==================== WebSocket核心接口（前端页面） =====================
@sock.route('/ws/frontend')
def handle_frontend_websocket(ws):
    """处理前端页面的WebSocket连接（修复鉴权逻辑）"""
    # 修复点：放宽鉴权逻辑，兼容referer为空的场景（本地访问/直接打开页面）
    env = ws.environ
    referer = env.get('HTTP_REFERER', '')
    host = env.get('HTTP_HOST', '')
    remote_addr = env.get('REMOTE_ADDR', '')

    # 允许的情况：1.referer包含当前host 2.本地访问（127.0.0.1/localhost） 3.referer为空（直接打开页面）
    is_allowed = False
    if referer and referer.startswith(f'http://{host}'):
        is_allowed = True
    elif remote_addr in ['127.0.0.1', 'localhost'] or referer == '':
        is_allowed = True

    if not is_allowed:
        print(f"❌ 非法前端WebSocket连接，来源：{referer}，IP：{remote_addr}")
        ws.close(403, 'Forbidden')
        return

    print("✅ 前端页面WebSocket连接成功")
    with frontend_conn_lock:
        frontend_ws_connections.append(ws)

        # 连接成功后，先推送队列中未发送的消息
        if pending_frontend_messages:
            print(f"📤 新前端连接，推送{len(pending_frontend_messages)}条待发消息")
            for pending_msg in pending_frontend_messages[:]:
                try:
                    ws.send(pending_msg)
                    pending_frontend_messages.remove(pending_msg)
                except:
                    continue

    try:
        # 保持连接，接收前端心跳（无消息则阻塞）
        while True:
            data = ws.receive()
            if not data:
                break
            # 处理前端心跳包
            if data == "ping":
                ws.send("pong")
    except Exception as e:
        print(f"❌ 前端WebSocket异常：{e}")
    finally:
        with frontend_conn_lock:
            if ws in frontend_ws_connections:
                frontend_ws_connections.remove(ws)
        print("❌ 前端页面WebSocket连接断开")


# ==================== 启动入口 =====================
if __name__ == '__main__':
    # 关键配置：debug=False + use_reloader=False + threaded=True
    app.run(
        host='0.0.0.0',
        port=5000,
        debug=False,
        use_reloader=False,
        threaded=True  # 开启多线程，支持并发WebSocket连接
    )