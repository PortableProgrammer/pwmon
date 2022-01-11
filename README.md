# pwmon

`pwmon.py` is a quick and dirty python script to demonstrate how to pull information from the Tesla Powerwall API and push it up to New Relic. I use it on my home solar system. If you want to use the weather bits you need to create an account at [openweathermap.org](https://openweathermap.org/).  

## Contents
There are five files in this repo.  The only one you need is `pwmon.py`.

The other four are to make it easy to run in a container. This is how I do it.

`Dockerfile` is the Dockerfile. Not much to say about this one.

`Makefile` is because I like shortcuts.  `make image` and `make container` are the two rules you want.

`env.list` is the list of environment variables `pwmon.py` wants to see in order to run.  
If you want to run `pwmon.py` from the CLI, one [good trick](https://stackoverflow.com/q/19331497) is

```
export $(grep -v '^#' env.list | xargs)
pwmon.py
```

`requirements.txt` pulls in `tenacity` (a retry library - the Powerwall API has some atrociously low rate-limit) and `tesla_powerwall`.


This code is licensed under Apache 2.0. Essentially - do what you want with it but neither I nor New Relic are to be held liable for it. This is hacked-together demo code and it works for me at home but that's as far as I've taken it. 


## Usage
### From the CLI

```
eosborne@host pwmon % export $(grep -v '^#' env.list | xargs)
eosborne@host pwmon % ./pwmon.py
submitted at 2022-01-11 08:57:20.332818 return code 0
{'common': {'attributes': {'app.name': 'solar',
                           'mode': 'Self_Consumption',
                           'poll_timestamp': 1641909439380,
                           'status': 'Connected'},
...
```


### From Docker

Build the image:
```
eosborne@host:~/prog/pwmon$ make image
.....
REPOSITORY   TAG       IMAGE ID       CREATED        SIZE
pwmon        latest    de412555d252   17 hours ago 277MB                                                                                                                                      
```

Start a container:
```
eosborne@host:~/prog/pwmon$ make container
docker run -d --restart unless-stopped --name pwmon --env-file env.list pwmon
974d312084d6ab79b65813dc485d18f3a110bcadc73967480b9273d9eadf3da5
```

Check that it works (you get one log line per minute):

```
eosborne@host:~/prog/pwmon$ docker logs pwmon
submitted at 2022-01-11 14:00:42.418491 return code 0
```

