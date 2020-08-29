p = re.compile(r"(?im)^\.a(n?)\s+((?:.|\n)*) fin$")


@borg.on(events.InlineQuery)
async def handler(event):
    query = event.text  # .lower()
    m = p.match(query)
    if (not await util.isAdmin(event)) or m == None:
        #print("inline rejected: " + query)
        # util.ix()
        # embed(using='asyncio')
        return
    print("inline accepted: " + query)
    builder = event.builder
    #result = builder.article('aget', text=m.group(2), link_preview=False)
    command = m.group(2)
    shell = True
    cwd = util.dl_base + "Inline " + str(uuid.uuid4()) + '/'
    Path(cwd).mkdir(parents=True, exist_ok=True)
    sp = (subprocess.run(command,
                         shell=shell,
                         cwd=cwd,
                         text=True,
                         executable='zsh' if shell else None,
                         stderr=subprocess.STDOUT,
                         stdout=subprocess.PIPE))
    output = sp.stdout
    output = f"The process exited {sp.returncode}." if output == '' else output

    rtext = builder.article('Text', text=output, link_preview=False)
    rfiles = [rtext]
    files = list(Path(cwd).glob('*'))
    files.sort()
    for f in files:
        if not f.is_dir():  # and not any(s in p.name for s in ('.torrent', '.aria2')):
            file_add = f.absolute()
            base_name = str(os.path.basename(file_add))
            ext = f.suffix
            # https://files.lilf.ir/Alice_Puzzle_Land.pdf
            # rfiles.append(builder.document(file_add, type='document', title="boo"))
            # embed()
            # if ext == '.mp3' or ext == '.m4a' or ext == '.m4b':
            #file_add = 'http://82.102.11.148:8080//tmp/Pharrell%20Williams%20-%20Despicable%20Me.c.c.mp3'
            #rfiles.append(builder.document(file_add, type='audio'))
            # rfiles.append(builder.document(file_add, type='document', text='hi 8')) #, title=base_name, description='test 36'))

    # deleting interferes with sending it
    await util.remove_potential_file(cwd, None)
    # NOTE: You should always answer, but we want plugins to be able to answer
    #       too (and we can only answer once), so we don't always answer here.
    await event.answer(rfiles, cache_time=0, private=True)  # returns true
    # util.ix()
    # embed(using='asyncio')
