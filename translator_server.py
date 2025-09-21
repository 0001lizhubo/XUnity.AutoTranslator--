import os
import re
import json
import time
from flask import Flask, request
from gevent.pywsgi import WSGIServer
from urllib.parse import unquote
from queue import Queue
import concurrent.futures
import requests

# 启用虚拟终端序列，支持 ANSI 转义代码，允许在终端显示彩色文本
os.system('')

# 配置文件路径
CONFIG_PATH = 'config.json'
dict_path='用户替换字典.json' # 替换字典路径。如果不需要使用替换字典，请将此变量留空（设为 None 或空字符串 ""）

# 默认配置
default_config = {
    
}

# 读取配置文件
def load_config():
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            config = json.load(f)
            # 验证必要配置项
            required_keys = ['SF_BASE_URL', 'SF_MODEL_TYPE', 'SF_API_TOKEN']
            for key in required_keys:
                if key not in config:
                    print(f"\033[31m错误：配置文件缺少必要项 {key}，使用默认配置。\033[0m")
                    return default_config
            return config
    except FileNotFoundError:
        print(f"\033[33m警告：配置文件 {CONFIG_PATH} 未找到，创建默认配置文件。\033[0m")
        # 创建默认配置文件
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(default_config, f, indent=4, ensure_ascii=False)
        return default_config
    except json.JSONDecodeError:
        print(f"\033[31m错误：配置文件 {CONFIG_PATH} JSON 格式错误，请检查配置文件。\033[0m")
        return default_config
    except Exception as e:
        print(f"\033[31m读取配置文件时发生未知错误: {e}\033[0m")
        return default_config

# 加载配置
config = load_config()

# SiliconFlow API 配置参数
SF_BASE_URL = config["SF_BASE_URL"]  # SiliconFlow API 请求地址
SF_MODEL_TYPE = config["SF_MODEL_TYPE"]  # 使用的 SiliconFlow 模型
SF_API_TOKEN = config["SF_API_TOKEN"]  # SiliconFlow API Token，请替换为您自己的 Token

# 提示词 (Prompt) 配置
prompt= '''
你是资深本地化专家，负责将游戏日文文本译为简体中文。接收文本后，按以下要求翻译：
翻译范围：翻译普通日文文本，保留原文叙述风格。
保留格式：保留转义字符、格式标签、换行符等非日文文本内容。
翻译原则：忠实准确，确保语义无误；对露骨性描写，可直白粗俗表述，不删减篡改；对双关语等特殊表达，找目标语言等效表达，保原作意图风格。
文本类型：游戏文本含角色对话、旁白、武器及物品名称、技能描述、格式标签、换行符、特殊符号等。
以下是待翻译的游戏文本：
''' # 基础提示词，用于指导模型进行翻译

app = Flask(__name__) # 创建 Flask 应用实例

# 读取提示字典
prompt_dict= {} # 初始化提示字典为空字典
if dict_path: # 检查是否配置了字典路径
    try:
        with open(dict_path, 'r', encoding='utf8') as f: # 尝试打开字典文件
            tempdict = json.load(f) # 加载 JSON 字典数据
            # 按照字典 key 的长度从长到短排序，确保优先匹配长 key，避免短 key 干扰长 key 的匹配
            sortedkey = sorted(tempdict.keys(), key=lambda x: len(x), reverse=True)
            for i in sortedkey:
                prompt_dict[i] = tempdict[i] # 将排序后的字典数据存入 prompt_dict
    except FileNotFoundError:
        print(f"\033[33m警告：字典文件 {dict_path} 未找到。\033[0m") # 警告用户字典文件未找到
    except json.JSONDecodeError:
        print(f"\033[31m错误：字典文件 {dict_path} JSON 格式错误，请检查字典文件。\033[0m") # 错误提示 JSON 格式错误
    except Exception as e:
        print(f"\033[31m读取字典文件时发生未知错误: {e}\033[0m") # 捕获其他可能的文件读取或 JSON 解析错误

def contains_japanese(text):
    """
    检测文本中是否包含日文字符。

    Args:
        text (str): 待检测的文本。

    Returns:
        bool: 如果文本包含日文字符，则返回 True；否则返回 False。
    """
    pattern = re.compile(r'[\u3040-\u3096\u309D-\u309F\u30A1-\u30FA\u30FC-\u30FE]') # 日文字符的 Unicode 范围正则表达式
    return pattern.search(text) is not None # 使用正则表达式搜索文本中是否包含日文字符

# 获得文本中包含的字典词汇
def get_dict(text):
    """
    从文本中提取出在提示字典 (prompt_dict) 中存在的词汇及其翻译。

    Args:
        text (str): 待处理的文本。

    Returns:
        dict: 一个字典，key 为在文本中找到的字典原文，value 为对应的译文。
              如果文本中没有找到任何字典词汇，则返回空字典。
    """
    res={} # 初始化结果字典
    for key in prompt_dict.keys(): # 遍历提示字典中的所有原文 (key)
        if key in text: # 检查当前原文 (key) 是否出现在待处理文本中
            res.update({key:prompt_dict[key]}) # 如果找到，则将该原文及其译文添加到结果字典中
            text=text.replace(key,'')   # 从文本中移除已匹配到的字典原文，避免出现长字典包含短字典导致重复匹配的情况。
        if text=='': # 如果文本在替换过程中被清空，说明所有文本内容都已被字典词汇覆盖，提前结束循环
            break
    return res # 返回提取到的字典词汇和译文

request_queue = Queue()  # 创建请求队列，用于异步处理翻译请求。

def handle_translation(text, translation_queue):
    """
    处理翻译请求的核心函数。

    Args:
        text (str): 待翻译的文本。
        translation_queue (Queue): 用于存放翻译结果的队列。
    """
    text = unquote(text) # 对接收到的文本进行 URL 解码，还原原始文本内容

    max_retries = 5  # 增加最大重试次数至 5 次，以应对模型输出不稳定的情况
    retries = 0  # 初始化重试次数计数器

    MAX_THREADS = 30 # 最大线程数限制
    queue_length = request_queue.qsize()
    number_of_threads = max(1, min(queue_length // 4, MAX_THREADS))

    special_chars = ['，', '。', '？','...'] # 定义句末特殊字符列表

    text_end_special_char = None
    if text[-1] in special_chars:
        text_end_special_char = text[-1]

    special_char_start = "「"
    special_char_end = "」"
    has_special_start = text.startswith(special_char_start)
    has_special_end = text.endswith(special_char_end)

    if has_special_start and has_special_end:
        text = text[len(special_char_start):-len(special_char_end)]

    # SiliconFlow 模型参数配置
    model_params = {
        "temperature": 0.7,
        "max_tokens": 512,
    }

    try:
        # 在重试循环外部获取字典，因为字典是固定的，不需要每次重试都重新获取
        dict_inuse = get_dict(text)
        base_prompt = prompt
        if dict_inuse: # 如果字典中有匹配项，则将字典信息添加到基础提示词中
            base_prompt += f'\n在翻译中使用以下字典,字典的格式为{{\'原文\':\'译文\'}}\n{dict_inuse}'

        # 开始重试循环，使用同一个 base_prompt 进行多次尝试
        while retries < max_retries:
            messages_test = [
                {"role": "system", "content": base_prompt},
                {"role": "user", "content": text}
            ]

            payload = {
                "model": SF_MODEL_TYPE,
                "messages": messages_test,
                **model_params
            }

            headers = {
                "Authorization": SF_API_TOKEN,
                "Content-Type": "application/json"
            }

            with concurrent.futures.ThreadPoolExecutor(max_workers=number_of_threads) as executor:
                future_to_trans = {executor.submit(requests.post, SF_BASE_URL + "/chat/completions", json=payload, headers=headers)}
                for future in concurrent.futures.as_completed(future_to_trans):
                    try:
                        response_test = future.result()
                        response_test.raise_for_status()
                        response_json = response_test.json()
                        translations = response_json["choices"][0]["message"]["content"]

                        # print(f'{base_prompt}\n{translations}') # 打印提示词和翻译结果

                        if translations.startswith('\n') and not text.startswith('\n'):
                            translations = translations.lstrip('\n')

                        if has_special_start and has_special_end:
                            if not translations.startswith(special_char_start):
                                translations = special_char_start + translations
                            if not translations.endswith(special_char_end):
                                translations = translations + special_char_end

                        translation_end_special_char = None
                        if translations[-1] in special_chars:
                            translation_end_special_char = translations[-1]

                        # 修正句末标点符号
                        if text_end_special_char and translation_end_special_char:
                            if text_end_special_char != translation_end_special_char:
                                translations = translations[:-1] + text_end_special_char
                        elif text_end_special_char and not translation_end_special_char:
                            translations += text_end_special_char
                        elif not text_end_special_char and translation_end_special_char:
                            translations = translations[:-1]

                        contains_japanese_characters = contains_japanese(translations)

                    except requests.exceptions.RequestException as e:
                        retries += 1
                        print(f"API请求失败或超时，正在进行第 {retries} 次重试... {e}")
                        if retries >= max_retries:
                            raise e
                        time.sleep(1)
                        continue # 跳过本次循环，进行下一次重试
                    except (KeyError, IndexError) as e: # 增加对 IndexError 的捕获，防止 JSON 结构异常
                        retries += 1
                        print(f"API响应格式错误，正在进行第 {retries} 次重试... {e}, 响应内容: {response_test.text if 'response_test' in locals() else 'N/A'}")
                        if retries >= max_retries:
                            raise e
                        time.sleep(1)
                        continue
                    except Exception as e:
                        retries += 1
                        print(f"处理API响应时发生未知错误，正在进行第 {retries} 次重试... {e}")
                        if retries >= max_retries:
                            raise e
                        time.sleep(1)
                        continue

            # 在本次重试的翻译完成后进行检查
            if not contains_japanese_characters:
                # 翻译成功，跳出重试循环
                break
            else:
                # 翻译结果包含日文，需要重试
                retries += 1
                print(f"\033[31m检测到译文中包含日文字符，正在进行第 {retries} 次重试...\033[0m")
                if retries >= max_retries:
                    print("\033[31m达到最大重试次数，翻译失败。\033[0m")
                    translation_queue.put(False)
                    return
                time.sleep(1) # 等待 1 秒后进行下一次重试

        # 打印最终翻译结果
        print(f"\033[36m[译文]\033[0m:\033[31m {translations}\033[0m")
        print("-------------------------------------------------------------------------------------------------------")
        translation_queue.put(translations)

    except Exception as e:
        print(f"API请求最终失败：{e}")
        translation_queue.put(False)

# 定义 Flask 路由，处理 "/translate" GET 请求
@app.route('/translate', methods=['GET'])
def translate():
    """
    Flask 路由函数，处理 "/translate" GET 请求。
    """
    text = request.args.get('text')
    print(f"\033[36m[原文]\033[0m \033[35m{text}\033[0m")

    translation_queue = Queue()
    request_queue.put_nowait(text)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        future = executor.submit(handle_translation, text, translation_queue)

        try:
            future.result(timeout=30)
        except concurrent.futures.TimeoutError:
            print("翻译请求超时，重新翻译...")
            return "[请求超时] " + text, 500

    translation = translation_queue.get()
    try:
        request_queue.get_nowait() # 尝试移除已处理的请求
    except:
        pass # 如果队列为空则忽略

    if isinstance(translation, str):
        translation = translation.replace('\\n', '\n')
        return translation
    else:
        return translation, 500

def main():
    """
    主函数，启动 Flask 应用和 gevent 服务器。
    """
    print("\033[31m服务器在 http://127.0.0.1:4000 上启动\033[0m")
    http_server = WSGIServer(('127.0.0.1', 4000), app, log=None, error_log=None)
    http_server.serve_forever()

if __name__ == '__main__':
    main()



