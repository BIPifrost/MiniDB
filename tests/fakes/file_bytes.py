"""仅测试使用：从已持有的文件句柄读物理字节，不读缓存、不另开文件。"""
def read_file_bytes(file_manager):
    stream = file_manager._handle
    position = stream.tell()
    try:
        stream.seek(0)
        return stream.read()
    finally:
        stream.seek(position)
