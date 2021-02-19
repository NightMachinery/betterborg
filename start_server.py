import sys
import uvicorn
from uvicorn.config import LOGGING_CONFIG
try:
    from IPython import embed
except:
    pass

def main():
    root_path = ''
    # if len(sys.argv) >= 2:
    #     root_path = sys.argv[1]
    ##
    # %(name)s : uvicorn, uvicorn.error, ... . Not insightful at all.
    LOGGING_CONFIG["formatters"]["access"]["fmt"] = '%(asctime)s %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'
    LOGGING_CONFIG["formatters"]["default"]["fmt"] = "%(asctime)s %(levelprefix)s %(message)s"

    date_fmt = "%Y-%m-%d %H:%M:%S"
    LOGGING_CONFIG["formatters"]["default"]["datefmt"] = date_fmt
    LOGGING_CONFIG["formatters"]["access"]["datefmt"] = date_fmt
    ##
    uvicorn.run("stdborg:app", host="127.0.0.1", port=5922, log_level="info", proxy_headers=True, root_path=root_path)

if __name__ == '__main__':
    main()