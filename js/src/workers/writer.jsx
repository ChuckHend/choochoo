
import log from "loglevel";


// a worker that receives dictionaries and sends them to the server.
// data are accumulated and only sent when there's a pause in updates
// (so that editing doesn't result in a constant stream of posts).

// there is no flagging of errors back to the caller (the source of events).

const pause_ms = 1000;
let timeout;

let data = {};


const close = self.close;

self.close = () => {
  write();
  close();
};


function queue(event) {
    Object.entries(event.data).forEach(([key, value]) => {
        data[key] = value;
    });
}


function write() {

    function onError(response) {
        response.text().then((msg) => log.error(msg));
    }

    function onSuccess(response) {
        data = {};
        log.debug('written');
    }

    if (Object.keys(data).length > 0) {
        Object.entries(data).forEach(([key, value]) => {
            log.debug(`write ${key}:${value}`);
        });
        fetch('/api/diary/statistics',
            {method: 'put',
                  headers: {'Accept': 'application/json', 'Content-Type': 'application/json'},
                  body: JSON.stringify(data)})
            .then((response) => {
                if (response.ok) {
                    onSuccess(response);
                } else {
                    onError(response);
                }
            })
            .catch(onError);
    }
}


self.addEventListener('message', (event) => {
    if (timeout !== undefined) clearTimeout(timeout);
    queue(event);
    timeout = setTimeout(write, pause_ms);
});
