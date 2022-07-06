import json
import poplib
import re
import time
from datetime import datetime
from email.header import decode_header
from email.parser import Parser
from email.utils import parseaddr
from queue import Queue
from threading import Thread
from urllib import parse

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings()

# github info
USERNAME = ""
PASSWD = ""
KEYWORD = ""
PROXY = {"http": "218.252.244.104:80"}  # https://free.kuaidaili.com/free/inha/

# email info
EMAIL_ACCOUNT = ""
AUTH_CODE = ""  # must authorization code
POP3_SSL_SERVER = "pop.qq.com"
# IMAP_SSL_SERVER = "imap.qq.com"
PROTOCOL = "pop3"


class EmailReceiver:
    def __init__(self, email_account, auth_code, pop3_ssl_server, send_login_time, protocol="pop3"):
        self.email_account = email_account
        self.auth_code = auth_code
        self.pop3_ssl_server = pop3_ssl_server
        self.send_login_time = send_login_time

        self.email_total_number = None

        self.session = self.login_pop3() if protocol == "pop3" else None
        if not self.session:
            raise ConnectionError("[Email Receiver] failed connect to the email server")

    def login_pop3(self):
        a = poplib.POP3_SSL(self.pop3_ssl_server)
        a.user(self.email_account)
        a.pass_(self.auth_code)
        resp, mails, octets = a.list()
        self.email_total_number = len(mails)
        print(f"[Email Receiver] the number of email is: {self.email_total_number}")
        return a if resp.decode("utf-8") == "+OK" else None

    def logout(self):
        if self.session:
            self.session.quit()

    @staticmethod
    def decode_str(s):
        value, charset = decode_header(s)[0]
        if charset:
            value = value.decode(charset)
        return value

    @staticmethod
    def _is_latest_email(content, send_login_time) -> bool:
        date = content.get("Received", "")
        ret = re.search(r"(?:\d+:){2,}?\d+", date)
        if not ret:
            print(f"[Email Receiver] get the latest email recv time failed, date: {date}")
            return False
        recv_email_time = ret.group()
        time_diff = datetime.strptime(recv_email_time, "%H:%M:%S") - datetime.strptime(send_login_time, "%H:%M:%S")
        print(f"recv: {recv_email_time}, login: {send_login_time}, {time_diff}")
        if time_diff.days < 0:
            print("[Email Receiver] the latest email received was not after logged into github")
        return time_diff.days == 0

    def _is_github_verify_email(self, content) -> bool:
        subject = self.decode_str(content.get("Subject", ""))
        from_ = self.decode_str(content.get("From", ""))
        ret = re.search(r"\[GitHub] Please verify your device", subject)
        if not ret:
            print(f"[Email Receiver] the latest email is not from github, subject: {subject}, From: {from_}")
            return False
        return True

    @staticmethod
    def get_email_content(session, total_number):
        print(f"[Email Receiver] curr total_number is: {total_number}")
        resp, lines, octets = session.retr(total_number)
        msg_content = b"\r\n".join(lines).decode("utf-8", "ignore")
        content = Parser().parsestr(msg_content)
        return content

    def get_verification_code(self):
        content = self.get_email_content(self.session, self.email_total_number)
        flag = False
        for index in range(5):
            if self._is_github_verify_email(content) and self._is_latest_email(content, self.send_login_time):
                flag = True
                break
            time.sleep(10)

            temp_session = poplib.POP3_SSL(self.pop3_ssl_server)
            temp_session.user(self.email_account)
            temp_session.pass_(self.auth_code)
            emails, _ = temp_session.stat()
            if emails > self.email_total_number:
                content = self.get_email_content(temp_session, emails)
            temp_session.quit()

        verification_code = re.search(r"Verification code: (\d+)", str(content))
        if flag and verification_code:
            return verification_code.groups()[0]
        print("[Email Receiver] get github verification code failed, try 5 times")


class GithubCrawl:
    def __init__(self, username, passwd, keyword):
        self.username = username
        self.passwd = passwd

        self.queue = Queue()
        self.session = requests.Session()
        self.result = []

        self.threads = 5
        self.login_time = datetime.now().strftime("%H:%M:%S")
        self.output_file = "./temp.txt"
        self.login_url = "https://github.com/login"
        self.post_url = "https://github.com/session"
        self.verify_device_url = "https://github.com/sessions/verified-device"
        self.search_url = f"https://github.com/search?q={keyword}&type=code"
        self.headers = {
            "Referer": "https://github.com/",
            "Host": "github.com",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/66.0.3359.139 Safari/537.36",
        }
        self.proxy = PROXY
        self.proxy = None

        # fiddler proxy
        # self.proxy = {'http': 'http://127.0.0.1:8888', 'https': 'http://127.0.0.1:8888'}

    @staticmethod
    def _parse_content(tags):
        content = ""
        for tag in tags:
            content += tag.text
        return content

    def _get_token(self):
        resp = self.session.get(self.login_url, headers=self.headers, verify=False, proxies=self.proxy)
        soup = BeautifulSoup(resp.text, "lxml")
        token = soup.find("input", attrs={"name": "authenticity_token"}).get("value")
        return token

    def _get_verification_code(self):
        print("[Github Crawl] need to get the verification code from the mailbox, please wait")
        email_receiver = EmailReceiver(EMAIL_ACCOUNT, AUTH_CODE, POP3_SSL_SERVER, self.login_time)
        verification_code = email_receiver.get_verification_code()
        email_receiver.logout()
        if not verification_code:
            raise ConnectionError("[Github Crawl] get verification code failed")
        return verification_code

    def login(self, token):
        post_data = {
            'commit': 'Sign in',
            'utf8': '✓',
            'login': self.username,
            'password': self.passwd,
            'authenticity_token': token
        }
        resp = self.session.post(self.post_url, data=post_data, headers=self.headers, verify=False, proxies=self.proxy)
        soup = BeautifulSoup(resp.text, "lxml")
        if resp.status_code == 200:
            print("[Github Crawl] start trying to set up a session to github")

            # when the account first login to the device, should get code from mail
            # print(soup.title.text)
            if re.search("Where software is built", soup.title.text, re.I):
                sec_token = soup.find("input", attrs={"name": "authenticity_token"}).get("value")
                verify_data = {
                    "authenticity_token": sec_token,
                    "otp": self._get_verification_code()
                }
                resp = self.session.post(self.verify_device_url, data=verify_data, headers=self.headers, verify=False, proxies=self.proxy)
                if resp.status_code == 200:
                    print("[Github Crawl] successful verify device")
            print("[Github Crawl] successful set up a session to github")
            self.get_urls()

    def get_urls(self):
        resp = self.session.get(self.search_url, headers=self.headers, verify=False, proxies=self.proxy)
        soup = BeautifulSoup(resp.text, "lxml")
        if re.search("sign in", soup.title.text, re.I):
            raise ConnectionError("[Github Crawl] the session is closed, please check network or add proxy!")
        # print(soup.title)
        total_pages = soup.find(attrs={"aria-label": "Pagination"}).text.split(" ")[-2]
        for i in range(1, int(total_pages) + 1):
            _url = self.search_url + f"&p={i}"
            print(f"[Github Crawl] add the url to queue: {_url}")
            self.queue.put(_url)

    def get_data(self):
        while True:
            if self.queue.empty():
                break
            url = self.queue.get()
            print(f"[Github Crawl] get url: {url}")
            self.parse_search_page(url)

    def parse_search_page(self, url):
            resp = self.session.get(url, headers=self.headers, verify=False, proxies=self.proxy)
            soup = BeautifulSoup(resp.text, "lxml")
            items = soup.find_all(class_="code-list-item")
            if not items:
                print(f"[Github Crawl] not found data in the page {url}...")
                return
            print(f"[Github Crawl] start parse url: {url}")
            for item in items:
                text_small = item.find(class_="text-small").text.strip().split("/")
                lang = item.find(attrs={"itemprop": "programmingLanguage"})
                data = {
                    "author_favicon": item.find("img").attrs["src"],
                    "author": text_small[0].strip(),
                    "repository": text_small[1].strip(),
                    "filename": item.find(class_="text-normal").text.strip(),
                    "filepath": parse.urljoin("https://github.com", item.find(class_="text-normal").a.attrs["href"]),
                    "content": self._parse_content(item.find_all(class_="blob-code")),
                    "language": lang.text if lang else lang,
                    "updated_at": item.find(class_="updated-at").find(class_="no-wrap").attrs["datetime"]
                }
                print(data)
                self.result.append(json.dumps(data))

    def write_to_file(self):
        try:
            with open(self.output_file, "w", encoding="utf-8") as f:
                f.writelines([line + "\n" for line in self.result])
            print("[Github Crawl] finished...")
        except Exception as e:
            print("[Github Crawl] write result to file failed...")
            raise e

    def start(self):
        token = self._get_token()
        self.login(token)
        t_list = []
        for i in range(self.threads):
            t = Thread(target=self.get_data)
            t_list.append(t)
            t.start()
        for t in t_list:
            t.join()
        print("[Github Crawl] all task finished...")
        self.write_to_file()


def main():
    crawler = GithubCrawl(USERNAME, PASSWD, KEYWORD)
    crawler.start()


if __name__ == '__main__':
    main()

# if __name__ == '__main__':
#     login_time = datetime.now().strftime("%H:%M:%S")
#     qq_email = EmailReceiver(EMAIL_ACCOUNT, AUTH_CODE, POP3_SSL_SERVER, "10:58:54")
#     code = qq_email.get_verification_code()
#     print(f"code is: {code}")
#     qq_email.logout()
