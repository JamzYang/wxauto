from wxauto import WeChat


def main() -> None:
    wx = WeChat()
    wx.SendMsg('你好，杨！', '全能战士测试群')
    # wx.SendFiles(r'C:\path\to\file.txt', '好友昵称')

    messages = wx.GetAllMessage()
    for msg in messages:
        print(msg.content)


if __name__ == '__main__':
    main()
