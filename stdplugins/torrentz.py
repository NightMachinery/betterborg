import cfscrape  # https://github.com/Anorov/cloudflare-scrape
from brish import z, zq, zs
import requests
from datetime import datetime
from bs4 import BeautifulSoup
from uniborg.util import admin_cmd, humanbytes, embeda, embed2, ix
from IPython import embed

@borg.on(admin_cmd(  # pylint:disable=E0602
    pattern=".tz (torrentz2\.eu|idop\.se)?\s*(.*)"
))
async def _(event):
    if event.fwd_from:
        return
    start = datetime.now()
    # await event.edit("Processing ...")
    input_type = event.pattern_match.group(1) or 'torrentz2.eu' #or "idop.se"
    input_str = event.pattern_match.group(2)
    logger.info(f"{input_type}: {input_str}")  # pylint:disable=E0602
    search_results = []
    # ix() ; embed(using='asyncio')
    if input_type == "torrentz2.eu":
        search_results = search_torrentz_eu(input_str)
    elif input_type == "idop.se":
        search_results = search_idop_se(input_str)
    # logger.info(search_results)  # pylint:disable=E0602
    output_str = ""
    i = 0
    for result in search_results:
        if i > 10:
            break
        magnet = z('hash2magnet {result["hash"]}').outrs
        message_text = "👉 <a href=https://t.me/TorrentSearchRoBot?start=" + result["hash"] +  ">" + result["title"] + ": " + "</a>" + " \r\n"
        # message_text += "<a href=https://t.me/spiritwellbot?start=" + z('base64', cmd_stdin=zs('magnet2torrent {magnet}')).outrs +  ">" +'Get hash torrent!'  + ": " + "</a>" + " \r\n"

        # Telegram's HTML doesn't support magnet hrefs.
        message_text += " Hash Magnet: " + magnet + "\r\n"
        message_text += " Size: " + result["size"] + "\r\n"
        # message_text += " Uploaded " + result["date"] + "\r\n"
        message_text += " Seeds: " + \
            result["seeds"] + "\r\n Peers: " + result["peers"] + " \r\n"
        message_text += "\r\n"
        output_str += message_text
        i = i + 1
    end = datetime.now()
    ms = (end - start).seconds
    await event.reply(
        f"Scrapped {input_type} for {input_str} in {ms} seconds. Obtained Results: \n {output_str}",
        link_preview=False,
        parse_mode="html"
    )


def search_idop_se(search_query):
    r = []
    url = "https://idope.se/search/{}/".format(search_query)
    raw_json = requests.get(url).json()
    print(f"idop.se json: {raw_json}")  # pylint:disable=E0602
    results = raw_json["result"]["items"]
    for item in results:
        """ The content scrapped on 24.09.2018 22:56:45
        """
        title = item["name"]
        hash = item["info_hash"]
        age = item["create_time"]
        size = item["length"]
        seeds = str(item["seeds"])
        r.append({
            "title": title,
            "hash": hash,
            "age": age,
            "size": humanbytes(size),
            "seeds": seeds,
            "peers": "NA"
        })
    return r


def search_torrentz_eu(search_query):
    r = []
    url = "https://torrentz2.eu/search?safe=1&f=" + search_query + ""
    # scraper = cfscrape.create_scraper()  # returns a CloudflareScraper instance
    # raw_html = scraper.get(url).content
    raw_html = z('curlfull.js {url}').out
    # print(f"tz2 html: {raw_html}")
    soup = BeautifulSoup(raw_html, "html.parser")
    results = soup.find_all("div", {"class": "results"})
    # print(results)
    if len(results) > 0:
        results = results[0]
        items = results.find_all("dl")
        for item in items:
            # print(item)
            """The content scrapped on 23.06.2018 15:40:35
            """
            dt = item.find_all("dt")[0]
            dd = item.find_all("dd")[0]
            #
            try:
                link_and_text = dt.find_all("a")[0]
                link = link_and_text.get("href")[1:]
                title = link_and_text.get_text()
                span_elements = dd.find_all("span")
                date = span_elements[1].get_text()
                size = span_elements[2].get_text()
                seeds = span_elements[3].get_text()
                peers = span_elements[4].get_text()
                #
                r.append({
                    "title": title,
                    "hash": link,
                    "date": date,
                    "size": size,
                    "seeds": seeds,
                    "peers": peers
                })
            except:
                pass
            
    return r
