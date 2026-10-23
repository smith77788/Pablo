"""Драйверы транспорта WB Chat (реализации WBChatTransport).

mock — в памяти, для разработки и тестов; real — заглушка до поставки протокола.
Выбор драйвера — через config.WB_CHAT_DRIVER и фабрику transport.build_transport.
"""
